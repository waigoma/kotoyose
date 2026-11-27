use super::*;
use axum::body::{Body, to_bytes};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tower::ServiceExt;

#[test]
fn authorization_is_random_and_rejects_ambiguous_headers() {
    let access = Access::new(43210);
    let now = Instant::now();
    let code = access.issue(now).unwrap();
    let token = access.exchange(&code, now).unwrap().unwrap();
    for value in [&code, &token] {
        assert_eq!(URL_SAFE_NO_PAD.decode(value).unwrap().len(), 32);
    }
    assert_ne!(code, token);
    let mut request = Request::post("/api/settings")
        .header(header::HOST, &access.host)
        .header(header::AUTHORIZATION, format!("Bearer {token}"))
        .header(header::ORIGIN, &access.origin)
        .header(header::CONTENT_TYPE, "application/json; charset=utf-8")
        .body(Body::empty())
        .unwrap();
    assert_eq!(access.check(&request), Ok(true));
    for (name, value, status) in [
        (header::HOST, access.host.as_str(), StatusCode::FORBIDDEN),
        (
            header::AUTHORIZATION,
            "Bearer other",
            StatusCode::UNAUTHORIZED,
        ),
        (header::ORIGIN, "null", StatusCode::FORBIDDEN),
        (
            header::CONTENT_TYPE,
            "text/plain",
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
        ),
    ] {
        let mut duplicate = Request::builder()
            .method(Method::POST)
            .uri("/api/settings")
            .body(Body::empty())
            .unwrap();
        *duplicate.headers_mut() = request.headers().clone();
        duplicate
            .headers_mut()
            .append(name, HeaderValue::from_str(value).unwrap());
        assert_eq!(access.check(&duplicate), Err(status));
    }
    *request.uri_mut() = "http://elsewhere.invalid/api/settings".parse().unwrap();
    assert_eq!(access.check(&request), Err(StatusCode::FORBIDDEN));
}

#[test]
fn codes_are_independent_single_use_and_expire_at_two_minutes() {
    let access = Access::new(43210);
    let now = Instant::now();
    let first = access.issue(now).unwrap();
    let second = access.issue(now).unwrap();
    assert_ne!(first, second);
    let token = access
        .exchange(&first, now + CODE_TTL - Duration::from_millis(1))
        .unwrap()
        .unwrap();
    assert!(
        access
            .exchange(&first, now + CODE_TTL - Duration::from_millis(1))
            .unwrap()
            .is_none()
    );
    assert!(access.exchange(&second, now + CODE_TTL).unwrap().is_none());
    assert!(!access.accepts(&first));
    assert!(access.accepts(&token));
}

fn test_router(home: PathBuf) -> (Arc<Access>, Arc<Control>, Router) {
    let access = Arc::new(Access::new(43210));
    let state = Arc::new(Control::new(|| {}));
    let (commands, _inbox) = mpsc::channel(1);
    let (activity, _requests) = watch::channel(Instant::now());
    let app = router(Web {
        home,
        state: state.clone(),
        commands,
        access: access.clone(),
        activity,
        component_runtime: None,
        login: LoginJobs::default(),
        confirmations: Arc::default(),
    });
    (access, state, app)
}

async fn body_json(response: Response) -> serde_json::Value {
    let raw = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
    serde_json::from_slice(&raw).unwrap()
}

async fn call_api(
    app: &Router,
    access: &Access,
    token: &str,
    path: &str,
    body: Option<serde_json::Value>,
) -> Response {
    let mut request = Request::builder()
        .uri(path)
        .header(header::HOST, &access.host)
        .header(header::AUTHORIZATION, format!("Bearer {token}"));
    let body = match body {
        Some(value) => {
            request = request
                .method(Method::POST)
                .header(header::ORIGIN, &access.origin)
                .header(header::CONTENT_TYPE, "application/json");
            Body::from(value.to_string())
        }
        None => Body::empty(),
    };
    app.clone()
        .oneshot(request.body(body).unwrap())
        .await
        .unwrap()
}

#[tokio::test]
async fn unrestricted_api_rejects_approval_without_a_confirmation_and_unknown_offers() {
    let home = tempfile::tempdir().unwrap();
    let (access, _, app) = test_router(home.path().into());
    let now = Instant::now();
    let code = access.issue(now).unwrap();
    let token = access.exchange(&code, now).unwrap().unwrap();
    let path = "/api/provider-declarations/grok-build";
    let sha256 = "ab".repeat(32);

    for (body, expected) in [
        (
            json!({"sha256": sha256, "capability": "unrestricted"}),
            StatusCode::CONFLICT,
        ),
        (
            json!({"sha256": sha256, "capability": "unrestricted", "nonce": "unissued"}),
            StatusCode::CONFLICT,
        ),
        (
            json!({"sha256": sha256, "capability": "unrestricted", "confirm_unrestricted": true}),
            StatusCode::UNPROCESSABLE_ENTITY,
        ),
    ] {
        assert_eq!(
            call_api(&app, &access, &token, path, Some(body))
                .await
                .status(),
            expected
        );
    }
    let response = call_api(
        &app,
        &access,
        &token,
        &format!("{path}/confirmation"),
        Some(json!({"sha256": sha256})),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert!(!home.path().join("provider-declarations/approved").exists());
}

#[tokio::test]
async fn corrupt_policy_loads_for_repair_and_only_one_concurrent_save_commits() {
    let home = tempfile::tempdir().unwrap();
    let raw = b"invalid policy";
    std::fs::write(home.path().join("policy.json"), raw).unwrap();
    let (access, control, app) = test_router(home.path().into());
    let now = Instant::now();
    let code = access.issue(now).unwrap();
    let token = access.exchange(&code, now).unwrap().unwrap();
    let response = call_api(&app, &access, &token, "/api/policy", None).await;
    assert_eq!(response.status(), StatusCode::OK);
    let data = body_json(response).await;
    assert_eq!(data["revision"], crate::link::fingerprint(raw));
    assert!(data["error"].is_string());
    assert_eq!(
        data["policy"],
        serde_json::to_value(Policy::default()).unwrap()
    );

    let edits = ["alpha", "beta"].map(|name| {
        json!({
            "revision": data["revision"],
            "policy": Policy { logins: vec![name.into()], ..Policy::default() },
        })
    });
    let [first, second] =
        edits.map(|edit| call_api(&app, &access, &token, "/api/policy", Some(edit)));
    let (first, second) = tokio::join!(first, second);
    let expected = match (first.status(), second.status()) {
        (StatusCode::OK, StatusCode::CONFLICT) => "alpha",
        (StatusCode::CONFLICT, StatusCode::OK) => "beta",
        statuses => panic!("unexpected save statuses: {statuses:?}"),
    };
    let data = body_json(call_api(&app, &access, &token, "/api/policy", None).await).await;
    assert!(data["error"].is_null());
    assert_eq!(data["policy"]["logins"], json!([expected]));
    let changes = *control.changes().borrow();
    assert_eq!(changes.policy, 1);
    assert!(!changes.saving_policy);
}

#[tokio::test]
async fn only_one_settings_save_commits_for_a_shared_revision() {
    let home = tempfile::tempdir().unwrap();
    let (access, control, app) = test_router(home.path().into());
    let now = Instant::now();
    let code = access.issue(now).unwrap();
    let token = access.exchange(&code, now).unwrap().unwrap();
    let data = body_json(call_api(&app, &access, &token, "/api/settings", None).await).await;
    let edits = [120, 240].map(|seconds| {
        json!({
            "revision": data["revision"], "runner_idle_seconds": seconds,
        })
    });
    let [first, second] =
        edits.map(|edit| call_api(&app, &access, &token, "/api/settings", Some(edit)));
    let (first, second) = tokio::join!(first, second);
    let expected = match (first.status(), second.status()) {
        (StatusCode::OK, StatusCode::CONFLICT) => 120,
        (StatusCode::CONFLICT, StatusCode::OK) => 240,
        statuses => panic!("unexpected save statuses: {statuses:?}"),
    };
    let idle = Duration::from_secs(expected);
    assert_eq!(state::idle(home.path()).unwrap(), idle);
    assert_eq!(control.changes().borrow().idle, Some((1, idle)));
}

#[tokio::test]
async fn router_enforces_authentication_and_update_methods() {
    let home = tempfile::tempdir().unwrap();
    let (access, _, app) = test_router(home.path().to_owned());
    let now = Instant::now();
    let code = access.issue(now).unwrap();
    let payload = json!({"code": code}).to_string();
    let send = |method: Method,
                path: &'static str,
                host: &str,
                token: Option<&str>,
                origin: Option<&str>,
                body: &str| {
        let mut request = Request::builder()
            .method(method)
            .uri(path)
            .header(header::HOST, host);
        if let Some(token) = token {
            request = request.header(header::AUTHORIZATION, token);
        }
        if let Some(origin) = origin {
            request = request.header(header::ORIGIN, origin);
        }
        if !body.is_empty() {
            request = request.header(header::CONTENT_TYPE, "application/json");
        }
        app.clone()
            .oneshot(request.body(Body::from(body.to_owned())).unwrap())
    };
    for (method, host, origin, body, expected) in [
        (
            Method::GET,
            access.host.as_str(),
            Some(access.origin.as_str()),
            payload.as_str(),
            StatusCode::METHOD_NOT_ALLOWED,
        ),
        (
            Method::POST,
            "rebind.invalid",
            Some(access.origin.as_str()),
            payload.as_str(),
            StatusCode::FORBIDDEN,
        ),
        (
            Method::POST,
            access.host.as_str(),
            Some("http://elsewhere.invalid"),
            payload.as_str(),
            StatusCode::FORBIDDEN,
        ),
        (
            Method::POST,
            access.host.as_str(),
            None,
            payload.as_str(),
            StatusCode::FORBIDDEN,
        ),
        (
            Method::POST,
            access.host.as_str(),
            Some(access.origin.as_str()),
            "",
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
        ),
    ] {
        let response = send(method, "/api/session", host, None, origin, body)
            .await
            .unwrap();
        assert_eq!(response.status(), expected);
    }
    let response = send(
        Method::POST,
        "/api/session",
        &access.host,
        None,
        Some(&access.origin),
        &payload,
    )
    .await
    .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let session = body_json(response).await;
    let bearer = format!("Bearer {}", session["token"].as_str().unwrap());
    let response = send(
        Method::POST,
        "/api/session",
        &access.host,
        None,
        Some(&access.origin),
        &payload,
    )
    .await
    .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    for (method, path, host, token, origin, body, expected) in [
        (
            Method::GET,
            "/",
            access.host.as_str(),
            None,
            None,
            "",
            StatusCode::OK,
        ),
        (
            Method::GET,
            "/",
            "rebind.invalid",
            None,
            None,
            "",
            StatusCode::FORBIDDEN,
        ),
        (
            Method::GET,
            "/api/status",
            &access.host,
            None,
            None,
            "",
            StatusCode::UNAUTHORIZED,
        ),
        (
            Method::GET,
            "/api/status",
            &access.host,
            Some("Bearer wrong"),
            None,
            "",
            StatusCode::UNAUTHORIZED,
        ),
        (
            Method::GET,
            "/api/status",
            "rebind.invalid",
            Some(bearer.as_str()),
            None,
            "",
            StatusCode::FORBIDDEN,
        ),
        (
            Method::GET,
            "/api/status",
            &access.host,
            Some(bearer.as_str()),
            None,
            "",
            StatusCode::OK,
        ),
        (
            Method::HEAD,
            "/api/status",
            &access.host,
            Some(bearer.as_str()),
            None,
            "",
            StatusCode::METHOD_NOT_ALLOWED,
        ),
        (
            Method::OPTIONS,
            "/api/settings",
            &access.host,
            None,
            None,
            "",
            StatusCode::UNAUTHORIZED,
        ),
        (
            Method::POST,
            "/api/settings",
            &access.host,
            None,
            Some(access.origin.as_str()),
            "{}",
            StatusCode::UNAUTHORIZED,
        ),
        (
            Method::POST,
            "/api/settings",
            &access.host,
            Some(bearer.as_str()),
            Some("http://elsewhere.invalid"),
            "{}",
            StatusCode::FORBIDDEN,
        ),
        (
            Method::POST,
            "/api/settings",
            &access.host,
            Some(bearer.as_str()),
            None,
            "{}",
            StatusCode::FORBIDDEN,
        ),
        (
            Method::POST,
            "/api/settings",
            &access.host,
            Some(bearer.as_str()),
            Some(access.origin.as_str()),
            "",
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
        ),
        (
            Method::GET,
            "/api/enroll",
            &access.host,
            Some(bearer.as_str()),
            None,
            "",
            StatusCode::METHOD_NOT_ALLOWED,
        ),
        (
            Method::GET,
            "/api/settings",
            &access.host,
            Some(bearer.as_str()),
            Some(access.origin.as_str()),
            r#"{"runner_idle_seconds":0}"#,
            StatusCode::OK,
        ),
    ] {
        let response = send(method.clone(), path, host, token, origin, body)
            .await
            .unwrap();
        assert_eq!(response.status(), expected, "{method} {path}");
        assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
        assert_eq!(response.headers()[header::REFERRER_POLICY], "no-referrer");
        assert_eq!(response.headers()[header::CONTENT_SECURITY_POLICY], CSP);
        assert!(
            !response
                .headers()
                .contains_key(header::ACCESS_CONTROL_ALLOW_ORIGIN)
        );
    }
    assert!(!home.path().join("app.json").exists());
    let response = send(
        Method::POST,
        "/api/settings",
        &access.host,
        Some(&bearer),
        Some(&access.origin),
        r#"{"revision":"missing","runner_idle_seconds":0}"#,
    )
    .await
    .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert!(state::idle(home.path()).unwrap().is_zero());

    std::fs::write(home.path().join("app.json"), r#"{"codex_cli":"system"}"#).unwrap();
    let (revision, _) = state::settings_revision(home.path());
    let body = format!(r#"{{"revision":"{revision}","runner_idle_seconds":1}}"#);
    let response = send(
        Method::POST,
        "/api/settings",
        &access.host,
        Some(&bearer),
        Some(&access.origin),
        &body,
    )
    .await
    .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let settings = state::settings(home.path()).unwrap();
    assert_eq!(settings.runner_idle_seconds, 1);
    assert_eq!(settings.extra["codex_cli"], "system");
}

async fn raw_request(address: &str, message: &str) -> String {
    let mut stream = tokio::net::TcpStream::connect(address).await.unwrap();
    stream.write_all(message.as_bytes()).await.unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).await.unwrap();
    response
}

async fn open_page(url: &str) -> (&str, String) {
    let (origin, code) = url.split_once("/#").unwrap();
    let address = origin.strip_prefix("http://").unwrap();
    let body = json!({"code": code}).to_string();
    let message = format!(
        "POST /api/session HTTP/1.1\r\nHost: {address}\r\nOrigin: {origin}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    let response = raw_request(address, &message).await;
    assert!(response.starts_with("HTTP/1.1 200"));
    let (_, body) = response.split_once("\r\n\r\n").unwrap();
    let session: serde_json::Value = serde_json::from_str(body).unwrap();
    (address, session["token"].as_str().unwrap().into())
}

async fn heartbeat(address: &str, token: &str) -> String {
    raw_request(
        address,
        &format!(
            "GET /api/status HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {token}\r\n\r\n"
        ),
    )
    .await
}

#[tokio::test]
async fn idle_expiry_closes_loopback_and_rotates_the_token() {
    let home = tempfile::tempdir().unwrap();
    let state = Arc::new(Control::new(|| {}));
    let (commands, _inbox) = mpsc::channel(1);
    let mut first = Server::start(
        home.path().to_owned(),
        state.clone(),
        commands.clone(),
        LoginJobs::default(),
    )
    .await
    .unwrap();
    let first_url = first.open().unwrap();
    let (address, first_token) = open_page(&first_url).await;
    let unused = first.open().unwrap();
    let (_, unused) = unused.split_once("/#").unwrap();
    let before_heartbeat = Instant::now();
    first
        .activity
        .send_replace(before_heartbeat - IDLE + Duration::from_secs(30));
    assert!(
        heartbeat(address, &first_token)
            .await
            .starts_with("HTTP/1.1 200")
    );
    assert!(*first.activity.borrow() >= before_heartbeat);
    first.activity.send_replace(Instant::now() - IDLE);
    timeout(Duration::from_secs(2), first.wait()).await.unwrap();
    assert!(tokio::net::TcpStream::connect(address).await.is_err());
    assert!(!first.access.accepts(&first_token));
    assert!(
        first
            .access
            .exchange(unused, Instant::now())
            .unwrap()
            .is_none()
    );

    let second = Server::start(
        home.path().to_owned(),
        state,
        commands,
        LoginJobs::default(),
    )
    .await
    .unwrap();
    let second_url = second.open().unwrap();
    let (address, second_token) = open_page(&second_url).await;
    assert_ne!(first_token, second_token);
    assert!(
        heartbeat(address, &first_token)
            .await
            .starts_with("HTTP/1.1 401")
    );
    assert!(
        heartbeat(address, &second_token)
            .await
            .starts_with("HTTP/1.1 200")
    );
    let access = second.access.clone();
    second.finish().await;
    assert!(!access.accepts(&second_token));
}

#[tokio::test]
async fn desktop_opens_loopback_lazily_and_closes_on_exit() {
    tokio::task::LocalSet::new()
        .run_until(async {
            let home = tempfile::tempdir().unwrap();
            let control = Arc::new(Control::new(|| {}));
            let (commands, inbox) = mpsc::channel(8);
            let (_stop, shutdown) = watch::channel(false);
            let (notify, mut notices) = mpsc::unbounded_channel();
            let task = tokio::task::spawn_local(crate::desktop::run(
                home.path().to_owned(),
                control.clone(),
                commands.clone(),
                inbox,
                shutdown,
                crate::desktop::Mode::Tray,
                move |event| {
                    notify.send(event).unwrap();
                },
            ));
            tokio::task::yield_now().await;
            assert!(notices.try_recv().is_err());
            assert!(!control.status().runner);

            commands.send(Command::Open("tray_menu")).await.unwrap();
            let url = match notices.recv().await.unwrap() {
                crate::desktop::Notice::Browser(url) => url,
                crate::desktop::Notice::Error(error) => panic!("{error}"),
            };
            commands
                .send(Command::Open("duplicate_instance"))
                .await
                .unwrap();
            let other_url = match notices.recv().await.unwrap() {
                crate::desktop::Notice::Browser(url) => url,
                crate::desktop::Notice::Error(error) => panic!("{error}"),
            };
            let logs = crate::local_log::tail(home.path()).unwrap();
            assert!(
                logs.iter()
                    .any(|line| line.ends_with("settings opened (tray_menu)"))
            );
            assert!(
                logs.iter()
                    .any(|line| line.ends_with("settings opened (duplicate_instance)"))
            );
            assert_ne!(url, other_url);
            let (address, token) = open_page(&url).await;
            let (other_address, other_token) = open_page(&other_url).await;
            assert_eq!(address, other_address);
            assert_ne!(token, other_token);
            let response = heartbeat(address, &token).await;
            assert!(response.starts_with("HTTP/1.1 200"));
            assert!(response.contains(r#""unregistered""#));
            assert!(response.contains(r#""runner_bytes":0"#));

            let (reply, result) = oneshot::channel();
            commands
                .send(Command::Action {
                    action: Action::Exit { restart: false },
                    interrupt: false,
                    reply: Some(reply),
                })
                .await
                .unwrap();
            assert!(result.await.unwrap().is_ok());
            assert!(matches!(
                task.await.unwrap().unwrap(),
                crate::desktop::Exit::Stop
            ));
            assert!(tokio::net::TcpStream::connect(address).await.is_err());
        })
        .await;
}
