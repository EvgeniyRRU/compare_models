def test_schema_has_required_tables_and_identity():
    from support_bot.db.base import Base
    from support_bot.db.models import Ticket

    assert set(Base.metadata.tables) == {
        "users",
        "tickets",
        "ticket_events",
        "ticket_drafts",
        "processed_updates",
    }
    assert Ticket.__table__.c.id.identity is not None
    assert Ticket.__table__.c.version.nullable is False
    assert Ticket.__table__.c.post_synced_version.nullable is False
    constraints = {c.name for c in Ticket.__table__.constraints}
    assert {"ck_ticket_assignment", "ck_ticket_closed", "ck_ticket_reminder"} <= constraints
