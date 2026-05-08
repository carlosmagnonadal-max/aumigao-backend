import os

os.environ.setdefault("EXPO_PUBLIC_DEMO_MODE", "false")
os.environ.setdefault("DEMO_MODE", "false")

from app.core.database import SessionLocal
from app.models.walker_profile import WalkerProfile


def main():
    with SessionLocal() as db:
        rows = db.query(WalkerProfile).order_by(WalkerProfile.created_at.desc()).all()
        print(f"total_walker_profiles={len(rows)}")
        for profile in rows:
            print(
                " | ".join([
                    f"id={profile.id}",
                    f"user_id={profile.user_id}",
                    f"nome={profile.full_name}",
                    f"status={profile.status}",
                    f"active_as_walker={profile.active_as_walker}",
                    f"created_at={profile.created_at}",
                ])
            )


if __name__ == "__main__":
    main()
