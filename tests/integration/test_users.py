import pytest

from support_bot.domain.errors import BusinessError, Forbidden
from support_bot.services.users import UserService

pytestmark = pytest.mark.integration


async def test_first_start_bootstraps_even_after_other_interaction_only_once(db, settings):
    settings.bootstrap_admin_ids = [100]
    service = UserService(db.sessions, settings)
    user = await service.touch(100, "Before")
    assert user.role is None and user.started_at is None
    user = await service.touch(100, "First", username="old", bootstrap=True)
    assert user.role == "admin" and user.started_at is not None
    started = user.started_at
    await service.set_role(user.id, user.id, None)
    user = await service.touch(100, "New", username=None, last_name="Last", bootstrap=True)
    assert user.role is None and user.started_at == started
    assert (user.first_name, user.username, user.last_name) == ("New", None, "Last")


async def test_role_management_requires_current_admin_and_valid_role(db, settings):
    service = UserService(db.sessions, settings)
    a = await service.touch(1, "A")
    b = await service.touch(2, "B", bootstrap=True)
    with pytest.raises(Forbidden):
        await service.set_role(a.id, b.id, "admin")
    settings.bootstrap_admin_ids = [3]
    admin = await service.touch(3, "Admin", bootstrap=True)
    assert (await service.set_role(admin.id, b.id, "developer")).role == "developer"
    with pytest.raises(BusinessError):
        await service.set_role(admin.id, b.id, "owner")
    assert (await service.get(b.id)).role == "developer"
