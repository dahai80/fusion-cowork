"""Stage 7 load — 1000 concurrent space creations across 10 tenants, no cross-tenant leakage.

@ pytest.mark.slow.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.slow]

from fusion_cowork.space.models import Space, SpaceMember, SpaceRole
from fusion_cowork.space.store import SpaceStore


@pytest.fixture
async def store(tmp_path):
    s = SpaceStore(data_dir=str(tmp_path))
    await s.initialize()
    yield s
    await s.close()


class TestLoadConcurrentSpaces:
    async def test_1000_concurrent_creates_10_tenants_no_leak(self, store):
        n_tenants = 10
        per_tenant = 100
        tenants = [f"load_t{i}" for i in range(n_tenants)]

        async def create_one(tenant: str, idx: int):
            sp = Space(name=f"{tenant}-sp{idx}", owner_id="loader", tenant_id=tenant)
            sp = await store.create_space(sp, tenant_id=tenant)
            m = SpaceMember(
                space_id=sp.id,
                user_id="loader",
                role=SpaceRole.OWNER.value,
                display_name="loader",
                tenant_id=tenant,
            )
            await store.add_member(m, tenant_id=tenant)
            return sp.id

        tasks = [create_one(tenants[i % n_tenants], i // n_tenants) for i in range(n_tenants * per_tenant)]
        ids = await asyncio.gather(*tasks)
        assert len(ids) == n_tenants * per_tenant
        assert len(set(ids)) == n_tenants * per_tenant

        # Exactly 100 per tenant, no cross-tenant leakage
        for t in tenants:
            spaces = await store.list_spaces(tenant_id=t, limit=500)
            assert len(spaces) == per_tenant, (
                f"租户 {t} 应有 {per_tenant} 空间, 实际 {len(spaces)}"  # i18n: user-facing
            )  # i18n: user-facing

        # Cross-tenant lookup finds nothing
        for t in tenants:
            other = next(x for x in tenants if x != t)
            other_spaces = await store.list_spaces(tenant_id=other, limit=500)
            my_spaces = await store.list_spaces(tenant_id=t, limit=500)
            my_ids = {s.id for s in my_spaces}
            other_ids = {s.id for s in other_spaces}
            assert my_ids.isdisjoint(other_ids)
