use std::{
    io,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, FromRequest, Path, Request, State},
    http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode, header},
    middleware::{self, Next},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use futures_util::StreamExt;
use hyper::server::conn::http1;
use hyper_util::{
    rt::{TokioIo, TokioTimer},
    service::TowerToHyperService,
};
use serde::{Deserialize, de::DeserializeOwned};
use serde_json::json;
use subtle::{Choice, ConstantTimeEq};
use tokio::{
    net::TcpListener,
    sync::{mpsc, oneshot, watch},
    task::{JoinHandle, JoinSet},
    time::{Instant, sleep_until, timeout},
};

use crate::{
    VERSION, components,
    control::Control,
    desktop::{Action, ActionError, Request as Command},
    local_log, messages, policy,
    state::{self, Policy, SAVE_LOCK, Settings},
};

mod confirmation;
mod login;
pub(super) use login::Jobs as LoginJobs;

const IDLE: Duration = Duration::from_secs(600);
const CODE_TTL: Duration = Duration::from_secs(120);
const MAX_CONNECTIONS: usize = 16;
const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'";

#[derive(Default)]
struct Credentials {
    codes: Vec<(String, Instant)>,
    sessions: Vec<String>,
}

struct Access {
    host: String,
    origin: String,
    credentials: Mutex<Option<Credentials>>,
}

fn random_token() -> io::Result<String> {
    let mut bytes = [0; 32];
    getrandom::fill(&mut bytes).map_err(io::Error::other)?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

impl Access {
    fn new(port: u16) -> Self {
        let host = format!("127.0.0.1:{port}");
        Self {
            origin: format!("http://{host}"),
            host,
            credentials: Mutex::new(Some(Credentials::default())),
        }
    }

    fn issue(&self, now: Instant) -> io::Result<String> {
        let code = random_token()?;
        let mut credentials = self.credentials.lock().expect("UI credentials");
        let credentials = credentials.as_mut().ok_or(io::ErrorKind::NotConnected)?;
        credentials.codes.retain(|(_, expiry)| *expiry > now);
        credentials.codes.push((code.clone(), now + CODE_TTL));
        Ok(code)
    }

    fn exchange(&self, code: &str, now: Instant) -> io::Result<Option<String>> {
        let mut credentials = self.credentials.lock().expect("UI credentials");
        let Some(credentials) = credentials.as_mut() else {
            return Ok(None);
        };
        credentials.codes.retain(|(_, expiry)| *expiry > now);
        let Some(index) = credentials
            .codes
            .iter()
            .position(|(entry, _)| bool::from(code.as_bytes().ct_eq(entry.as_bytes())))
        else {
            return Ok(None);
        };
        let session = random_token()?;
        credentials.codes.swap_remove(index);
        credentials.sessions.push(session.clone());
        Ok(Some(session))
    }

    fn accepts(&self, token: &str) -> bool {
        self.credentials
            .lock()
            .expect("UI credentials")
            .as_ref()
            .is_some_and(|credentials| {
                bool::from(
                    credentials
                        .sessions
                        .iter()
                        .fold(Choice::from(0), |valid, session| {
                            valid | token.as_bytes().ct_eq(session.as_bytes())
                        }),
                )
            })
    }

    fn close(&self) {
        self.credentials.lock().expect("UI credentials").take();
    }

    fn check(&self, request: &Request) -> Result<bool, StatusCode> {
        let headers = request.headers();
        if single(headers, header::HOST) != Some(&self.host)
            || request
                .uri()
                .authority()
                .is_some_and(|host| host.as_str() != self.host)
        {
            return Err(StatusCode::FORBIDDEN);
        }
        let path = request.uri().path();
        if !path.starts_with("/api/") && path != "/api" {
            return (request.method() == Method::GET)
                .then_some(false)
                .ok_or(StatusCode::METHOD_NOT_ALLOWED);
        }
        let exchange = path == "/api/session";
        if exchange {
            if request.method() != Method::POST {
                return Err(StatusCode::METHOD_NOT_ALLOWED);
            }
        } else {
            if !bearer_token(headers).is_some_and(|token| self.accepts(token)) {
                return Err(StatusCode::UNAUTHORIZED);
            }
        }
        match *request.method() {
            Method::GET => Ok(true),
            Method::POST | Method::DELETE => {
                if single(headers, header::ORIGIN) != Some(&self.origin) {
                    return Err(StatusCode::FORBIDDEN);
                }
                if request.method() == Method::DELETE {
                    return Ok(true);
                }
                let mime = single(headers, header::CONTENT_TYPE)
                    .and_then(|value| value.split(';').next())
                    .map(str::trim);
                if !mime.is_some_and(|value| value.eq_ignore_ascii_case("application/json")) {
                    return Err(StatusCode::UNSUPPORTED_MEDIA_TYPE);
                }
                Ok(!exchange)
            }
            _ => Err(StatusCode::METHOD_NOT_ALLOWED),
        }
    }
}

fn single(headers: &HeaderMap, name: HeaderName) -> Option<&str> {
    let all = headers.get_all(name);
    let mut values = all.iter();
    let value = values.next()?.to_str().ok();
    if values.next().is_none() { value } else { None }
}

fn bearer_token(headers: &HeaderMap) -> Option<&str> {
    single(headers, header::AUTHORIZATION)?.strip_prefix("Bearer ")
}

#[derive(Clone)]
struct Web {
    home: PathBuf,
    state: Arc<Control>,
    commands: mpsc::Sender<Command>,
    access: Arc<Access>,
    activity: watch::Sender<Instant>,
    component_runtime: Option<components::Runtime>,
    login: login::Jobs,
    confirmations: Arc<confirmation::Confirmations>,
}

async fn guard(State(web): State<Web>, request: Request, next: Next) -> Response {
    let mut response = match web.access.check(&request) {
        Ok(authenticated) => {
            if authenticated {
                web.activity.send_replace(Instant::now());
            }
            let path = request.uri().path();
            let installing = request.method() == Method::POST
                && path.starts_with("/api/provider-declarations/")
                && path.ends_with("/install");
            let deadline = Duration::from_secs(if installing { 95 } else { 45 });
            timeout(deadline, next.run(request))
                .await
                .unwrap_or_else(|_| {
                    ApiError::new(StatusCode::REQUEST_TIMEOUT, messages::WEB_TIMEOUT)
                        .into_response()
                })
        }
        Err(error) => error.into_response(),
    };
    let headers = response.headers_mut();
    for (name, value) in [
        (header::CONTENT_SECURITY_POLICY, CSP),
        (header::CACHE_CONTROL, "no-store"),
        (header::REFERRER_POLICY, "no-referrer"),
        (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
    ] {
        headers.insert(name, HeaderValue::from_static(value));
    }
    response
}

fn router(web: Web) -> Router {
    Router::new()
        .route("/", get(async || Html(include_str!("web/index.html"))))
        .route(
            "/console.css",
            get(async || {
                (
                    [(header::CONTENT_TYPE, "text/css; charset=utf-8")],
                    include_str!("../../src/kotoyose/static/console/console.css"),
                )
            }),
        )
        .route(
            "/style.css",
            get(async || {
                (
                    [(header::CONTENT_TYPE, "text/css; charset=utf-8")],
                    include_str!("web/style.css"),
                )
            }),
        )
        .route(
            "/app.js",
            get(async || {
                (
                    [(header::CONTENT_TYPE, "text/javascript; charset=utf-8")],
                    include_str!("web/app.js"),
                )
            }),
        )
        .route(
            "/providers.js",
            get(async || {
                (
                    [(header::CONTENT_TYPE, "text/javascript; charset=utf-8")],
                    include_str!("web/providers.js"),
                )
            }),
        )
        .route("/api/status", get(status))
        .route("/api/session", post(exchange_session))
        .route("/api/policy", get(load_policy).post(save_policy))
        .route("/api/settings", get(settings).post(save_settings))
        .route("/api/host-mcp", get(host_mcps).post(edit_host_mcp))
        .route(
            "/api/components",
            get(component_status).post(change_component),
        )
        .route("/api/components/check", get(check_components))
        .route("/api/provider-declarations", get(list_declarations))
        .route(
            "/api/provider-declarations/{id}",
            post(approve_declaration).delete(revoke_declaration),
        )
        .route(
            "/api/provider-declarations/{id}/confirmation",
            post(confirmation::review),
        )
        .route(
            "/api/provider-declarations/{id}/install",
            post(install_declaration),
        )
        .route("/api/login/status/{provider}", post(login::check))
        .route(
            "/api/login/{provider}",
            get(login::progress)
                .post(login::start)
                .delete(login::cancel),
        )
        .route("/api/login/{provider}/code", post(login::submit_code))
        .route("/api/logs", get(logs))
        .route("/api/enroll", post(enroll))
        .route("/api/action", post(action))
        .route("/api/update", get(update_progress).post(update_app))
        .route("/api/autostart", post(autostart))
        .route("/api/open-logs", post(open_logs))
        .layer(DefaultBodyLimit::max(1024 * 1024))
        .layer(middleware::from_fn_with_state(web.clone(), guard))
        .with_state(web)
}

pub struct Server {
    access: Arc<Access>,
    activity: watch::Sender<Instant>,
    stop: Option<oneshot::Sender<()>>,
    task: JoinHandle<()>,
}

struct PublishedPort(PathBuf);

impl Drop for PublishedPort {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

impl Server {
    pub(super) async fn start(
        home: PathBuf,
        state: Arc<Control>,
        commands: mpsc::Sender<Command>,
        login: LoginJobs,
    ) -> io::Result<Self> {
        let socket = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
        let port = socket.local_addr()?.port();
        let port_file = home.join(state::SETTINGS_PORT_FILE);
        state::replace_checked(
            &port_file,
            port.to_string().as_bytes(),
            state::protect_secret,
            || Ok(()),
        )
        .map_err(io::Error::other)?;
        let published_port = PublishedPort(port_file);
        let access = Arc::new(Access::new(port));
        let (activity, requests) = watch::channel(Instant::now());
        let (stop, stopped) = oneshot::channel();
        let web = Web {
            home,
            state,
            commands,
            access: access.clone(),
            activity: activity.clone(),
            component_runtime: components::Runtime::installed(),
            login,
            confirmations: Arc::default(),
        };
        let server_access = access.clone();
        let task = tokio::spawn(async move {
            let _published_port = published_port;
            serve(socket, router(web), server_access, requests, stopped).await;
        });
        Ok(Self {
            access,
            activity,
            stop: Some(stop),
            task,
        })
    }

    pub fn open(&self) -> io::Result<String> {
        let now = Instant::now();
        let code = self.access.issue(now)?;
        self.activity.send_replace(now);
        Ok(format!("{}/#{code}", self.access.origin))
    }

    pub fn finished(&self) -> bool {
        self.task.is_finished()
    }

    pub async fn wait(&mut self) {
        let _ = (&mut self.task).await;
    }

    pub async fn finish(mut self) {
        let _ = self.stop.take().expect("UI stop sender").send(());
        self.wait().await;
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.access.close();
        self.task.abort();
    }
}

async fn serve(
    socket: TcpListener,
    app: Router,
    access: Arc<Access>,
    mut activity: watch::Receiver<Instant>,
    mut stopping: oneshot::Receiver<()>,
) {
    let mut clients = JoinSet::new();
    let graceful = loop {
        let deadline = *activity.borrow_and_update() + IDLE;
        tokio::select! {
            biased;
            _ = &mut stopping => break true,
            _ = activity.changed() => {},
            _ = sleep_until(deadline) => break false,
            _ = clients.join_next(), if !clients.is_empty() => {},
            client = socket.accept(), if clients.len() < MAX_CONNECTIONS => {
                let Ok((stream, _)) = client else { break false };
                let service = TowerToHyperService::new(app.clone());
                clients.spawn(async move {
                    let _ = http1::Builder::new()
                        .timer(TokioTimer::new())
                        .header_read_timeout(Duration::from_secs(5))
                        .max_buf_size(16 * 1024)
                        .keep_alive(false)
                        .serve_connection(TokioIo::new(stream), service)
                        .await;
                });
            }
        }
    };
    access.close();
    drop(socket);
    if graceful {
        let _ = timeout(Duration::from_secs(1), async {
            while clients.join_next().await.is_some() {}
        })
        .await;
    }
}

struct ApiError {
    status: StatusCode,
    message: String,
}

impl ApiError {
    fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
        }
    }

    fn internal(message: impl Into<String>) -> Self {
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, message)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({ "error": self.message }))).into_response()
    }
}

struct Input<T>(T);

impl<S: Send + Sync, T: DeserializeOwned> FromRequest<S> for Input<T> {
    type Rejection = ApiError;

    async fn from_request(request: Request, state: &S) -> Result<Self, Self::Rejection> {
        Json::<T>::from_request(request, state)
            .await
            .map(|Json(value)| Self(value))
            .map_err(|error| ApiError::new(error.status(), messages::INVALID_INPUT))
    }
}

#[derive(Deserialize)]
struct SessionCode {
    code: String,
}

async fn exchange_session(
    State(web): State<Web>,
    Input(input): Input<SessionCode>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let now = Instant::now();
    let token = web
        .access
        .exchange(&input.code, now)
        .map_err(|_| ApiError::internal(messages::SETTINGS_OPEN))?
        .ok_or_else(|| ApiError::new(StatusCode::UNAUTHORIZED, messages::INVALID_CODE))?;
    web.activity.send_replace(now);
    Ok(Json(json!({ "token": token })))
}

async fn status(State(web): State<Web>) -> Json<serde_json::Value> {
    let state = web.state.status();
    let bytes = match state.memory.clone() {
        Some(probe) => tokio::task::spawn_blocking(move || probe())
            .await
            .ok()
            .flatten(),
        None => (!state.runner).then_some(0),
    };
    let update = web.state.update.view();
    let restart_needed = update.phase == crate::update::Phase::Deferred
        || crate::update::installed_change().is_some();
    Json(json!({
        "link": state, "version": VERSION, "runner_bytes": bytes,
        "build_id": crate::update::build_id(), "update": update,
        "can_update": crate::update::can_update(), "restart_needed": restart_needed,
        "enrolled": !crate::enrollment::needed(&web.home),
    }))
}

async fn policy_adapters(
    home: &std::path::Path,
) -> Result<(Vec<String>, Option<String>), ApiError> {
    let python = state::python().ok();
    let (mut adapters, warning) = match state::available_adapters(home, python.as_deref()).await {
        Ok(adapters) => (adapters, None),
        Err(components::AvailabilityError::Busy) => {
            return Err(ApiError::new(
                StatusCode::CONFLICT,
                messages::COMPONENT_BUSY,
            ));
        }
        Err(components::AvailabilityError::Failed) => (
            state::legacy_adapters(python.as_deref()),
            Some(messages::COMPONENT_PROBE_FAILED.into()),
        ),
    };
    adapters.extend(
        crate::declarations::installed_providers(home.to_owned())
            .await
            .iter()
            .filter_map(|row| row["provider"].as_str().map(str::to_owned)),
    );
    Ok((adapters, warning))
}

async fn load_policy(State(web): State<Web>) -> Result<Json<policy::Versioned>, ApiError> {
    let (available, warning) = policy_adapters(&web.home).await?;
    tokio::task::spawn_blocking(move || {
        let mut policy = policy::load(&web.home, available);
        policy.warnings.extend(warning);
        policy
    })
    .await
    .map(Json)
    .map_err(|_| ApiError::internal(messages::POLICY_LOAD))
}

#[derive(Deserialize)]
struct PolicyEdit {
    revision: String,
    policy: Policy,
}

fn save_error(error: anyhow::Error, status: StatusCode, message: String) -> ApiError {
    if error.is::<state::Conflict>() {
        ApiError::new(StatusCode::CONFLICT, error.to_string())
    } else {
        ApiError::new(status, message)
    }
}

async fn save_policy(
    State(web): State<Web>,
    Input(edit): Input<PolicyEdit>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let (available, warning) = policy_adapters(&web.home).await?;
    let lock = SAVE_LOCK.lock().await;
    let home = web.home.clone();
    let expected = edit.revision.clone();
    let (contents, mut warnings) = tokio::task::spawn_blocking(move || {
        policy::check_revision(&home, &expected)?;
        policy::validate(&home, edit.policy, available)
    })
    .await
    .map_err(|_| ApiError::internal(messages::POLICY_VALIDATE))?
    .map_err(|error| {
        let message = error.to_string();
        save_error(error, StatusCode::UNPROCESSABLE_ENTITY, message)
    })?;
    warnings.extend(warning);
    let applying = web.state.begin_policy_save();
    let revision = tokio::task::spawn_blocking(move || {
        // Keep the lock and publication with the write if the request goes away.
        let _lock = lock;
        let pending = applying;
        let revision = policy::save(&web.home, &edit.revision, &contents).map_err(|error| {
            save_error(
                error,
                StatusCode::INTERNAL_SERVER_ERROR,
                messages::POLICY_SAVE.into(),
            )
        })?;
        pending.commit();
        local_log::record(&web.home, local_log::Event::PolicySaved);
        Ok::<_, ApiError>(revision)
    })
    .await
    .map_err(|_| ApiError::internal(messages::POLICY_SAVE))??;
    Ok(Json(json!({ "revision": revision, "warnings": warnings })))
}

async fn host_mcps(State(web): State<Web>) -> Result<Json<serde_json::Value>, ApiError> {
    tokio::task::spawn_blocking(move || {
        let catalog = crate::host_mcp::catalog(&web.home);
        let approved = crate::host_mcp::approved(&web.home, &catalog.fingerprint);
        Json(
            json!({"enabled": cfg!(target_os = "linux"), "catalog": catalog, "approved": approved}),
        )
    })
    .await
    .map_err(|_| ApiError::internal("Could not load host MCP publications."))
}

async fn edit_host_mcp(
    State(web): State<Web>,
    Input(edit): Input<crate::host_mcp::Edit>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let lock = SAVE_LOCK.lock().await;
    tokio::task::spawn_blocking(move || {
        let _lock = lock;
        crate::host_mcp::edit(&web.home, edit)
    })
    .await
    .map_err(|_| ApiError::internal("Could not save MCP approval."))?
    .map_err(|error| ApiError::new(StatusCode::CONFLICT, error.to_string()))?;
    Ok(Json(json!({"saved": true})))
}

async fn settings(State(web): State<Web>) -> Result<Json<serde_json::Value>, ApiError> {
    tokio::task::spawn_blocking(move || {
        let (revision, settings) = state::settings_revision(&web.home);
        #[cfg(windows)]
        let (autostart, autostart_error) = match crate::autostart::enabled() {
            Ok(enabled) => (Some(enabled), None),
            Err(error) => (None, Some(error.to_string())),
        };
        #[cfg(not(windows))]
        let (autostart, autostart_error): (Option<bool>, Option<String>) = (None, None);
        Json(json!({
            "revision": revision,
            "runner_idle_seconds": settings.as_ref().ok().map(|s| s.runner_idle_seconds),
            "settings_error": settings.err().map(|_| messages::SETTINGS_READ),
            "autostart": autostart,
            "autostart_error": autostart_error,
            "can_open_logs": cfg!(windows),
        }))
    })
    .await
    .map_err(|_| ApiError::internal(messages::SETTINGS_LOAD))
}

#[derive(Deserialize)]
struct SettingsEdit {
    revision: String,
    #[serde(flatten)]
    settings: Settings,
}

async fn save_settings(
    State(web): State<Web>,
    Input(edit): Input<SettingsEdit>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let lock = SAVE_LOCK.lock().await;
    tokio::task::spawn_blocking(move || {
        let _lock = lock;
        let (current, previous) = state::settings_revision(&web.home);
        if current != edit.revision {
            return Err(ApiError::new(
                StatusCode::CONFLICT,
                state::Conflict.to_string(),
            ));
        }
        let seconds = edit.settings.runner_idle_seconds;
        let mut settings = previous.map_err(|_| {
            ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, messages::SETTINGS_READ)
        })?;
        let changed = settings.runner_idle_seconds != seconds;
        settings.runner_idle_seconds = seconds;
        let revision = settings.save(&web.home, &edit.revision).map_err(|error| {
            save_error(
                error,
                StatusCode::INTERNAL_SERVER_ERROR,
                messages::SETTINGS_SAVE.into(),
            )
        })?;
        if changed {
            web.state.update_idle(Duration::from_secs(seconds.into()));
            local_log::record(&web.home, local_log::Event::IdleSaved);
        }
        Ok(Json(
            json!({ "revision": revision, "runner_idle_seconds": seconds }),
        ))
    })
    .await
    .map_err(|_| ApiError::internal(messages::SETTINGS_SAVE))?
}

async fn component_status(State(web): State<Web>) -> Json<components::Status> {
    Json(
        components::status(
            &web.home,
            web.component_runtime.as_ref(),
            components::ProbeMode::Status,
            &web.state.repairs,
        )
        .await,
    )
}

async fn check_components(State(web): State<Web>) -> Json<components::Status> {
    Json(
        components::status(
            &web.home,
            web.component_runtime.as_ref(),
            components::ProbeMode::Check,
            &web.state.repairs,
        )
        .await,
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeclarationApproval {
    sha256: String,
    capability: String,
    nonce: Option<String>,
}

async fn list_declarations(State(web): State<Web>) -> Json<serde_json::Value> {
    Json(
        tokio::task::spawn_blocking(move || crate::declarations::list(&web.home))
            .await
            .unwrap_or_else(|_| json!({"offers": [], "error": "Could not load declarations."})),
    )
}

async fn approve_declaration(
    State(web): State<Web>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(body): Json<DeclarationApproval>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let unrestricted = body.capability == "unrestricted";
    if unrestricted {
        web.confirmations
            .confirm(&headers, &id, &body.sha256, body.nonce.as_deref())?;
    }
    tokio::task::spawn_blocking(move || {
        let result = crate::declarations::approve(
            &web.home,
            &id,
            &body.sha256,
            &body.capability,
            unrestricted,
        );
        if result.is_ok() {
            web.state.modify(|changes| changes.policy += 1);
        }
        result
    })
    .await
    .map_err(|_| ApiError::internal("Could not approve the provider declaration."))?
    .map(Json)
    .map_err(|error| ApiError::new(StatusCode::BAD_REQUEST, error.to_string()))
}

async fn revoke_declaration(
    State(web): State<Web>,
    Path(id): Path<String>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let home = web.home.clone();
    let revoked = id.clone();
    let view = tokio::task::spawn_blocking(move || crate::declarations::revoke(&home, &revoked))
        .await
        .map_err(|_| ApiError::internal("Could not revoke the provider declaration."))?
        .map_err(|error| ApiError::new(StatusCode::BAD_REQUEST, error.to_string()))?;
    web.state.modify(|changes| changes.policy += 1);
    let _ = web.login.cancel(&id).await;
    Ok(Json(view))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeclarationInstall {
    sha256: String,
}

async fn install_declaration(
    State(web): State<Web>,
    Path(id): Path<String>,
    Input(body): Input<DeclarationInstall>,
) -> Result<Json<serde_json::Value>, ApiError> {
    static LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
    let _lock = LOCK.try_lock().map_err(|_| {
        ApiError::new(
            StatusCode::CONFLICT,
            "A provider declaration installation is in progress.",
        )
    })?;
    let result = crate::declarations::install(&web.home, &id, &body.sha256)
        .await
        .map_err(|error| ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, error.to_string()))?;
    web.state.modify(|changes| changes.policy += 1);
    Ok(Json(result))
}

#[derive(Deserialize)]
struct ComponentEdit {
    provider: String,
    change: components::Change,
    #[serde(default)]
    interrupt: bool,
}

async fn change_component(
    State(web): State<Web>,
    Input(edit): Input<ComponentEdit>,
) -> Result<Response, ApiError> {
    let provider = components::provider(&edit.provider)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, messages::COMPONENT_UNKNOWN))?;
    let lock = components::COMPONENT_LOCK
        .try_write()
        .map_err(|_| ApiError::new(StatusCode::CONFLICT, messages::COMPONENT_BUSY))?;
    let (progress, receiver) = mpsc::channel(64);
    let (result, outcome) = oneshot::channel();
    let job = components::Job {
        runtime: web.component_runtime.clone(),
        provider,
        change: edit.change,
        progress,
        result,
        lock,
    };
    desktop_action(&web, Action::Components(Box::new(job)), edit.interrupt).await?;

    let stream = futures_util::stream::unfold(receiver, |mut receiver| async {
        let message = receiver.recv().await?;
        Some((components::Event::Progress { message }, receiver))
    })
    .chain(futures_util::stream::once(outcome).filter_map(|result| async { result.ok() }))
    .map(|event| {
        let mut json = serde_json::to_vec(&event).expect("serializable component event");
        json.push(b'\n');
        Ok::<_, io::Error>(Bytes::from(json))
    });
    Ok((
        [(header::CONTENT_TYPE, "application/x-ndjson; charset=utf-8")],
        Body::from_stream(stream),
    )
        .into_response())
}

async fn logs(State(web): State<Web>) -> Result<Json<Vec<String>>, ApiError> {
    let lines = tokio::task::spawn_blocking(move || local_log::tail(&web.home))
        .await
        .map_err(|_| ApiError::internal(messages::LOGS_LOAD))?
        .map_err(|_| ApiError::internal(messages::LOGS_LOAD))?;
    Ok(Json(lines))
}

#[derive(Deserialize)]
struct Enrollment {
    code: String,
    name: String,
    #[serde(default)]
    interrupt: bool,
}

async fn enroll(
    State(web): State<Web>,
    Input(edit): Input<Enrollment>,
) -> Result<Json<serde_json::Value>, ApiError> {
    request_action(
        &web,
        Action::Enroll {
            name: edit.name.trim().to_owned(),
            code: edit.code.trim().to_owned(),
        },
        edit.interrupt,
    )
    .await
}

#[derive(Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
enum Update {
    Download { sha256: String, defer: bool },
    Interrupt,
    Acknowledge,
}

async fn update_progress(State(web): State<Web>) -> Response {
    update_stream(web.state, StatusCode::OK)
}

fn update_stream(state: Arc<Control>, status: StatusCode) -> Response {
    use crate::update::Phase;
    use tokio::sync::broadcast::error::RecvError;

    let views = state.update.views();
    let initial = Some(state.update.view());
    let stream = futures_util::stream::unfold(
        (views, initial, false),
        |(mut views, initial, done)| async move {
            if done {
                return None;
            }
            let view = match initial {
                Some(view) => view,
                None => loop {
                    match views.recv().await {
                        Ok(view) => break view,
                        Err(RecvError::Lagged(_)) => continue,
                        Err(RecvError::Closed) => return None,
                    }
                },
            };
            let done =
                matches!(view.phase, Phase::Idle | Phase::Deferred) || view.expected_shutdown;
            let mut line = serde_json::to_vec(&view).expect("serializable update status");
            line.push(b'\n');
            Some((Ok::<_, io::Error>(Bytes::from(line)), (views, None, done)))
        },
    );
    (
        status,
        [(header::CONTENT_TYPE, "application/x-ndjson; charset=utf-8")],
        Body::from_stream(stream),
    )
        .into_response()
}

async fn update_app(
    State(web): State<Web>,
    headers: HeaderMap,
    Input(request): Input<Update>,
) -> Result<Response, ApiError> {
    let accepted = match request {
        Update::Download { sha256, defer } => web.state.update.request(&web.home, &sha256, defer),
        Update::Interrupt => web.state.update.activate(true),
        Update::Acknowledge => {
            web.state.update.acknowledge_shutdown();
            return Ok(StatusCode::NO_CONTENT.into_response());
        }
    };
    if !accepted {
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            messages::UPDATE_UNAVAILABLE,
        ));
    }
    if single(&headers, header::ACCEPT) == Some("application/x-ndjson") {
        return Ok(update_stream(web.state, StatusCode::ACCEPTED));
    }
    Ok(StatusCode::ACCEPTED.into_response())
}

#[derive(Deserialize)]
struct Exit {
    restart: bool,
    #[serde(default)]
    interrupt: bool,
}

async fn action(
    State(web): State<Web>,
    Input(exit): Input<Exit>,
) -> Result<Json<serde_json::Value>, ApiError> {
    if exit.restart
        && web.state.update.view().phase != crate::update::Phase::Deferred
        && crate::update::installed_change().is_none()
    {
        return Err(ApiError::new(
            StatusCode::CONFLICT,
            messages::RESTART_UNNEEDED,
        ));
    }
    request_action(
        &web,
        Action::Exit {
            restart: exit.restart,
        },
        exit.interrupt,
    )
    .await
}

async fn request_action(
    web: &Web,
    action: Action,
    interrupt: bool,
) -> Result<Json<serde_json::Value>, ApiError> {
    let message = desktop_action(web, action, interrupt).await?;
    Ok(Json(json!({ "message": message })))
}

async fn desktop_action(web: &Web, action: Action, interrupt: bool) -> Result<String, ApiError> {
    let (reply, result) = oneshot::channel();
    let result = match web.commands.try_send(Command::Action {
        action,
        interrupt,
        reply: Some(reply),
    }) {
        Ok(()) => result.await.unwrap_or(Err(ActionError::Unavailable)),
        Err(_) => Err(ActionError::Unavailable),
    };
    result.map_err(|error| match error {
        ActionError::Busy => ApiError::new(StatusCode::CONFLICT, messages::WORK_ACTIVE),
        ActionError::Unavailable => {
            ApiError::new(StatusCode::SERVICE_UNAVAILABLE, messages::APP_UNAVAILABLE)
        }
        ActionError::Failed(error) => ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, error),
    })
}

#[derive(Deserialize)]
struct Autostart {
    enabled: bool,
}

async fn autostart(Input(setting): Input<Autostart>) -> Result<Json<serde_json::Value>, ApiError> {
    #[cfg(windows)]
    {
        let message = tokio::task::spawn_blocking(move || crate::autostart::set(setting.enabled))
            .await
            .map_err(|_| ApiError::internal(messages::AUTOSTART_CHANGE))?
            .map_err(|error| ApiError::internal(format!("{error:#}")))?;
        Ok(Json(json!({ "message": message.ui() })))
    }
    #[cfg(not(windows))]
    {
        let _ = setting.enabled;
        Err(ApiError::new(
            StatusCode::NOT_IMPLEMENTED,
            messages::AUTOSTART_WINDOWS,
        ))
    }
}

async fn open_logs(
    State(web): State<Web>,
    Input(_): Input<serde_json::Value>,
) -> Result<Json<serde_json::Value>, ApiError> {
    #[cfg(windows)]
    {
        let folder = local_log::folder(&web.home);
        tokio::task::spawn_blocking(move || crate::tray::open_folder(&folder))
            .await
            .map_err(|_| ApiError::internal(messages::LOGS_OPEN))?
            .map_err(|error| ApiError::internal(format!("{error:#}")))?;
        Ok(Json(json!({})))
    }
    #[cfg(not(windows))]
    {
        let _ = web;
        Err(ApiError::new(
            StatusCode::NOT_IMPLEMENTED,
            messages::LOGS_WINDOWS,
        ))
    }
}

#[cfg(test)]
mod tests;
