"""CLI: grant admin to an existing user.

Usage: python -m recipe_normalizer.users.make_admin <email>
"""

import sys

from sqlalchemy import select

from recipe_normalizer.db import SessionLocal
from recipe_normalizer.users.models import User


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m recipe_normalizer.users.make_admin <email>")
    email = sys.argv[1].strip().lower()
    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.email == email)).first()
        if user is None:
            raise SystemExit(f"no user with email {email}")
        user.is_admin = True
        db.commit()
        print(f"{email} is now an admin")


if __name__ == "__main__":
    main()
