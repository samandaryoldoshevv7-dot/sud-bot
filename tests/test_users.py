from sqlalchemy import func, select

from app.models import User, UserRole, UserStatus
from app.services import groups as group_service
from app.services import users as user_service
from app.services.users import UserFilter
from tests.factories import make_employee, tg_user


async def test_registration_is_idempotent_and_updates_profile(session):
    u1 = await user_service.upsert_user(session, tg_user(555, "Ali", "Valiyev", "ali"), private_chat=True)
    u2 = await user_service.upsert_user(session, tg_user(555, "Ali", "Valiyev", "ali_new"), private_chat=True)
    assert u1.id == u2.id
    assert u2.username == "ali_new"
    assert u2.role == UserRole.EMPLOYEE and u2.status == UserStatus.PENDING
    assert (await session.execute(select(func.count(User.id)))).scalar_one() == 1


async def test_admin_role_comes_only_from_env(session):
    admin = await user_service.upsert_user(session, tg_user(1000), private_chat=True)
    assert admin.role == UserRole.ADMIN and admin.status == UserStatus.ACTIVE
    other = await user_service.upsert_user(session, tg_user(777), private_chat=True)
    assert other.role == UserRole.EMPLOYEE
    # A stale admin row is demoted when the env no longer lists it.
    other.role = UserRole.ADMIN
    await session.commit()
    await user_service.sync_admin_roles(session)
    await session.refresh(other)
    assert other.role == UserRole.EMPLOYEE
    # Admins cannot be deactivated from the bot.
    await user_service.set_status(session, admin.id, UserStatus.INACTIVE)
    await session.refresh(admin)
    assert admin.status == UserStatus.ACTIVE


async def test_employee_listing_and_search(session):
    await make_employee(session, 11, "Valiyev Ali")
    await make_employee(session, 12, "Karimova Dilnoza")
    await make_employee(session, 13, "Hasanov Bobur", active=False)
    users, total = await user_service.list_employees(session, UserFilter(status="active"), 0)
    assert total == 2
    users, total = await user_service.list_employees(session, UserFilter(query="dilnoza"), 0)
    assert total == 1 and users[0].full_name == "Karimova Dilnoza"
    users, total = await user_service.list_employees(session, UserFilter(query="13"), 0)
    assert total == 1
    counts = await user_service.employee_counts(session)
    assert counts == {"pending": 1, "active": 2, "inactive": 0, "total": 3}


async def test_group_registration_and_membership(session):
    admin = await user_service.upsert_user(session, tg_user(1000))
    group, created = await group_service.register_group(session, -100123, "Sud xodimlari", admin)
    assert created
    group2, created2 = await group_service.register_group(session, -100123, "Sud xodimlari 2", admin)
    assert not created2 and group2.id == group.id
    emp = await make_employee(session, 21, "Valiyev Ali")
    await group_service.mark_membership(session, group, emp, True)
    await group_service.mark_membership(session, group, emp, True)  # idempotent
    assert await user_service.is_member_of_active_group(session, emp.id)
    _members, total = await group_service.group_members(session, group.id, 0)
    assert total == 1
    await group_service.mark_membership(session, group, emp, False)
    assert not await user_service.is_member_of_active_group(session, emp.id)
