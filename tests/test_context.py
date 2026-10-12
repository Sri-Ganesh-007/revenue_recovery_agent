from app.db.database import SessionLocal
from app.repositories.recovery_repository import RecoveryRepository
from app.context.context_builder import RecoveryContextBuilder


def main():
    db = SessionLocal()

    try:
        repository = RecoveryRepository(db)
        builder = RecoveryContextBuilder(repository)

        context = builder.build("pay_004")

        print(context.model_dump_json(indent=2))

    finally:
        db.close()


if __name__ == "__main__":
    main()