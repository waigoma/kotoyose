use super::*;

const TTL: Duration = Duration::from_secs(60);
const MAX_PENDING: usize = 32;
const CONFIRM_AGAIN: &str =
    "Review the unrestricted warning again before approving this declaration.";

#[derive(Default)]
pub(super) struct Confirmations(Mutex<Vec<Pending>>);

struct Pending {
    session: String,
    id: String,
    sha256: String,
    nonce: String,
    expires: Instant,
}

impl Confirmations {
    fn issue(
        &self,
        session: &str,
        id: String,
        sha256: String,
        now: Instant,
    ) -> Result<String, ApiError> {
        let nonce = random_token()
            .map_err(|_| ApiError::internal("Could not issue an approval confirmation."))?;
        let mut pending = self.0.lock().expect("declaration confirmations");
        pending.retain(|entry| entry.expires > now && (entry.session != session || entry.id != id));
        if pending.len() >= MAX_PENDING {
            return Err(ApiError::new(
                StatusCode::TOO_MANY_REQUESTS,
                "Too many pending approval confirmations.",
            ));
        }
        pending.push(Pending {
            session: session.to_owned(),
            id,
            sha256,
            nonce: nonce.clone(),
            expires: now + TTL,
        });
        Ok(nonce)
    }

    fn consume(&self, session: &str, id: &str, sha256: &str, nonce: &str, now: Instant) -> bool {
        let mut pending = self.0.lock().expect("declaration confirmations");
        pending.retain(|entry| entry.expires > now);
        let Some(index) = pending.iter().position(|entry| {
            entry.session == session
                && entry.id == id
                && entry.sha256 == sha256
                && bool::from(entry.nonce.as_bytes().ct_eq(nonce.as_bytes()))
        }) else {
            return false;
        };
        pending.swap_remove(index);
        true
    }

    pub(super) fn confirm(
        &self,
        headers: &HeaderMap,
        id: &str,
        sha256: &str,
        nonce: Option<&str>,
    ) -> Result<(), ApiError> {
        if let (Some(session), Some(nonce)) = (bearer_token(headers), nonce)
            && self.consume(session, id, sha256, nonce, Instant::now())
        {
            return Ok(());
        }
        Err(ApiError::new(StatusCode::CONFLICT, CONFIRM_AGAIN))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Review {
    sha256: String,
}

pub(super) async fn review(
    State(web): State<Web>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Json(body): Json<Review>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let session = bearer_token(&headers)
        .ok_or_else(|| ApiError::new(StatusCode::UNAUTHORIZED, CONFIRM_AGAIN))?;
    let view = tokio::task::spawn_blocking(move || crate::declarations::list(&web.home))
        .await
        .map_err(|_| ApiError::internal("Could not load declarations."))?;
    let warning = offer_warning(&view, &id, &body.sha256).ok_or_else(|| {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "No matching unrestricted declaration awaits approval.",
        )
    })?;
    let nonce = web
        .confirmations
        .issue(session, id, body.sha256, Instant::now())?;
    Ok(Json(json!({"nonce": nonce, "warning": warning})))
}

fn offer_warning<'a>(view: &'a serde_json::Value, id: &str, sha256: &str) -> Option<&'a str> {
    let host = view["host_fingerprint"].as_str()?;
    if host.is_empty() || view["current_fingerprint"] != host {
        return None;
    }
    let offer = view["offers"].as_array()?.iter().find(|offer| {
        offer["id"] == id
            && offer["sha256"] == sha256
            && offer["capability"] == "unrestricted"
            && offer["status"] != "approved"
    })?;
    offer["rights"]
        .as_str()
        .filter(|warning| !warning.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_current_unrestricted_offer_provides_a_warning() {
        let mut view = json!({"host_fingerprint": "host", "current_fingerprint": "host", "offers": [{
            "id": "grok", "sha256": "sha", "capability": "unrestricted", "status": "pending", "rights": "warning"
        }]});
        assert_eq!(offer_warning(&view, "grok", "sha"), Some("warning"));
        assert_eq!(offer_warning(&view, "grok", "old-sha"), None);
        view["offers"][0]["capability"] = json!("read");
        assert_eq!(offer_warning(&view, "grok", "sha"), None);
        view["offers"][0]["capability"] = json!("unrestricted");
        view["current_fingerprint"] = json!("new-host");
        assert_eq!(offer_warning(&view, "grok", "sha"), None);
    }

    #[test]
    fn confirmations_are_bound_to_session_id_and_hash_single_use_and_expiring() {
        let pending = Confirmations::default();
        let now = Instant::now();
        let nonce = pending
            .issue("session", "grok".into(), "sha".into(), now)
            .ok()
            .unwrap();
        assert_eq!(URL_SAFE_NO_PAD.decode(&nonce).unwrap().len(), 32);
        assert!(!pending.consume("other", "grok", "sha", &nonce, now));
        assert!(!pending.consume("session", "other", "sha", &nonce, now));
        assert!(!pending.consume("session", "grok", "new-sha", &nonce, now));
        assert!(!pending.consume("session", "grok", "sha", "invalid", now));
        assert!(pending.consume(
            "session",
            "grok",
            "sha",
            &nonce,
            now + TTL - Duration::from_millis(1)
        ));
        assert!(!pending.consume("session", "grok", "sha", &nonce, now));

        let nonce = pending
            .issue("session", "grok".into(), "sha".into(), now)
            .ok()
            .unwrap();
        assert!(!pending.consume("session", "grok", "sha", &nonce, now + TTL));
    }
}
