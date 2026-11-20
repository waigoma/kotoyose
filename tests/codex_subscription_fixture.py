"""実サービスで利用できない JWT と subscription auth.json を生成する。"""

import base64
import json
import time

from kotoyose.codex_subscription import read_subscription


def subscription_auth(tmp_path, *, account="fixture-account", expiry=None, token_label="fixture", **values):
    claims = base64.urlsafe_b64encode(json.dumps({"exp": expiry if expiry is not None else time.time() + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": account}}).encode()).rstrip(b"=").decode()
    token = "eyJhbGciOiJub25lIn0." + claims + "." + token_label
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"access_token": token, "account_id": account, "refresh_token": "false-refresh-secret",
                   "id_token": "false-id-secret"}, **values}))
    path.chmod(0o600)
    return path, token


def subscription_job(tmp_path):
    path, _ = subscription_auth(tmp_path)
    return {"credential_name": "CODEX_CHATGPT_AUTH", "credential_value": "",
            "codex_auth_file": path, "codex_auth_identity": read_subscription(path).identity}
