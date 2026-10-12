"""Set or rotate a clinic's admin sign-up invite code.

The plain code is printed ONCE and never stored — only its hash is. Run from
the repo root:

    uv run backend/ops/scripts/set_clinic_invite_code.py "Shifa International"

Tell the team before running this against the shared Supabase project: it
either creates or overwrites that clinic's one invite-code row, so an old
code already given out stops working the moment this runs.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.models import Clinic, ClinicInviteCode
from app.db.session import SessionFactory
from app.verification.codes import generate_code, hash_code
from sqlalchemy import func, select


async def main(clinic_name: str) -> None:
    async with SessionFactory() as session:
        clinic = (
            await session.execute(
                select(Clinic).where(func.lower(Clinic.name) == clinic_name.lower())
            )
        ).scalar_one_or_none()
        if clinic is None:
            print(f"No clinic named {clinic_name!r}. Nothing changed.")
            return

        code = generate_code()
        row = await session.get(ClinicInviteCode, clinic.id)
        if row is None:
            session.add(
                ClinicInviteCode(clinic_id=clinic.id, code_hash=hash_code(code), is_active=True)
            )
        else:
            row.code_hash = hash_code(code)
            row.is_active = True
        await session.commit()

    print(f"Invite code for {clinic_name}: {code}")
    print("This is shown once. Give it to that clinic's admin now — it cannot be read back later.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: uv run backend/ops/scripts/set_clinic_invite_code.py "<clinic name>"')
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1]))
