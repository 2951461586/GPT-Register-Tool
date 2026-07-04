from dataclasses import dataclass


@dataclass
class MailboxAccount:
    email: str
    password: str = ""
    refresh_token: str = ""
    access_token: str = ""
    source: str = ""
    provider: str = "graph"
    order_no: str = ""
    token: str = ""
    seen_message_id: str = ""
    purchase_id: str = ""
    project_name: str = ""
    price: str = ""
    purchase_total_cost: str = ""
    balance_after: str = ""
