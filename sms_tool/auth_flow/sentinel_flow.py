"""Same-flow Sentinel issuance for ``authorize/continue``."""

from __future__ import annotations


def _authorize_continue_sentinel(
    session,
    did,
    proxy="",
    sentinel_token="",
    sentinel_so_token="",
):
    """Fetch a fresh, same-flow Sentinel challenge for API continue calls."""
    from ..sentinel import issue_sentinel_flow

    issued = issue_sentinel_flow(
        flow="authorize_continue",
        device_id=did,
        session=session,
        proxy=proxy,
        supplied_data={
            "sentinel_authorize_continue_token": sentinel_token,
            "sentinel_authorize_continue_so_token": sentinel_so_token,
        },
    )
    data = {
        "sentinel_authorize_continue_token": issued.token,
        "sentinel_authorize_continue_so_token": issued.so_token,
        "sentinel_source": "node_sdk_runner",
        "oai_did": issued.device_id,
    }
    return data, issued.token, issued.so_token
