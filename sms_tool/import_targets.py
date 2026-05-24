from .cpa_import import fetch_cpa_auth_files, import_cpa_session, import_cpa_sessions
from .sub2api_import import fetch_sub2api_auth_files, import_sub2api_session, import_sub2api_sessions


TARGET_CPA = "cpa"
TARGET_SUB2API = "sub2api"
TARGET_LABELS = {
    TARGET_CPA: "CPA",
    TARGET_SUB2API: "SUB2API",
}


def normalize_import_target(value):
    text = str(value or "").strip().lower().replace("-", "").replace("_", "")
    if text in {"sub2api", "subapi", "s2a"}:
        return TARGET_SUB2API
    return TARGET_CPA


def target_label(target):
    return TARGET_LABELS.get(normalize_import_target(target), "CPA")


def import_account_session(
    target,
    email="",
    session_file="",
    export_dir="",
    refresh=True,
    proxy=None,
    timeout=300,
    cpa_api_url="",
    cpa_api_token="",
    sub2api_url="",
    sub2api_token="",
    sub2api_email="",
    sub2api_password="",
    sub2api_group="",
    sub2api_group_ids=None,
    sub2api_proxy="",
    sub2api_proxy_id=None,
    sub2api_priority=None,
    sub2api_concurrency=None,
):
    target = normalize_import_target(target)
    if target == TARGET_SUB2API:
        return import_sub2api_session(
            email=email,
            session_file=session_file,
            export_dir=export_dir,
            refresh=refresh,
            proxy=proxy,
            timeout=timeout,
            api_url=sub2api_url,
            api_token=sub2api_token,
            login_email=sub2api_email,
            login_password=sub2api_password,
            group_name=sub2api_group,
            group_ids=sub2api_group_ids,
            proxy_name=sub2api_proxy,
            proxy_id=sub2api_proxy_id,
            priority=sub2api_priority,
            concurrency=sub2api_concurrency,
        )
    return import_cpa_session(
        email=email,
        session_file=session_file,
        export_dir=export_dir,
        refresh=refresh,
        proxy=proxy,
        timeout=timeout,
        api_url=cpa_api_url,
        api_token=cpa_api_token,
    )


def import_account_sessions(
    target,
    emails,
    export_dir="",
    workers=1,
    refresh=True,
    proxy=None,
    timeout=300,
    cpa_api_url="",
    cpa_api_token="",
    sub2api_url="",
    sub2api_token="",
    sub2api_email="",
    sub2api_password="",
    sub2api_group="",
    sub2api_group_ids=None,
    sub2api_proxy="",
    sub2api_proxy_id=None,
    sub2api_priority=None,
    sub2api_concurrency=None,
):
    target = normalize_import_target(target)
    if target == TARGET_SUB2API:
        return import_sub2api_sessions(
            emails,
            export_dir=export_dir,
            workers=workers,
            refresh=refresh,
            proxy=proxy,
            timeout=timeout,
            api_url=sub2api_url,
            api_token=sub2api_token,
            login_email=sub2api_email,
            login_password=sub2api_password,
            group_name=sub2api_group,
            group_ids=sub2api_group_ids,
            proxy_name=sub2api_proxy,
            proxy_id=sub2api_proxy_id,
            priority=sub2api_priority,
            concurrency=sub2api_concurrency,
        )
    return import_cpa_sessions(
        emails,
        export_dir=export_dir,
        workers=workers,
        refresh=refresh,
        proxy=proxy,
        timeout=timeout,
        api_url=cpa_api_url,
        api_token=cpa_api_token,
    )


def fetch_target_auth_files(
    target,
    cpa_api_url="",
    cpa_api_token="",
    sub2api_url="",
    sub2api_token="",
    sub2api_email="",
    sub2api_password="",
):
    target = normalize_import_target(target)
    if target == TARGET_SUB2API:
        return fetch_sub2api_auth_files(
            api_url=sub2api_url,
            api_token=sub2api_token,
            login_email=sub2api_email,
            login_password=sub2api_password,
        )
    return fetch_cpa_auth_files(cpa_api_url, cpa_api_token)
