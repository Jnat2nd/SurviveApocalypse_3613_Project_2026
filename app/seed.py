"""Seed admin, students, landlords, and demo listings."""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.database import drop_all, engine, ensure_db_and_tables
from app.models.listing import Listing
from app.models.user import User
from app.services.lifecycle import fields_for_status
from app.utilities.security import encrypt_password

logger = logging.getLogger(__name__)

EXISTING_USERS = [
    ("admin", "admin@example.com", "admin-dev", "admin", "Campus Admin", None, 0),
    (
        "student",
        "student@example.com",
        "stud-dev",
        "student",
        "Registered Student",
        "202012345",
        0,
    ),
    (
        "incoming",
        "incoming@example.com",
        "stud-dev",
        "student",
        "Provisional Incoming",
        None,
        1,
    ),
    ("landlord", "landlord@example.com", "land-dev", "landlord", "Landlord One", None, 0),
    (
        "landlord2",
        "landlord2@example.com",
        "land-dev",
        "landlord",
        "Landlord Two",
        None,
        0,
    ),
]

ADDITIONAL_STUDENTS = [
    ("amara.johnson", "amara.johnson@example.com", "stud-dev", "student", "Amara Johnson", "202100101", 0),
    ("liam.chen", "liam.chen@example.com", "stud-dev", "student", "Liam Chen", "202100102", 0),
    ("maya.patel", "maya.patel@example.com", "stud-dev", "student", "Maya Patel", "202100103", 0),
    ("noah.williams", "noah.williams@example.com", "stud-dev", "student", "Noah Williams", "202100104", 0),
    ("sophia.martinez", "sophia.martinez@example.com", "stud-dev", "student", "Sophia Martinez", "202100105", 0),
    ("ethan.brown", "ethan.brown@example.com", "stud-dev", "student", "Ethan Brown", "202100106", 0),
    ("aisha.rahman", "aisha.rahman@example.com", "stud-dev", "student", "Aisha Rahman", "202100107", 0),
    ("lucas.garcia", "lucas.garcia@example.com", "stud-dev", "student", "Lucas Garcia", "202100108", 0),
    ("grace.okafor", "grace.okafor@example.com", "stud-dev", "student", "Grace Okafor", "202100109", 0),
    ("oliver.nguyen", "oliver.nguyen@example.com", "stud-dev", "student", "Oliver Nguyen", "202100110", 0),
    ("emilia.smith", "emilia.smith@example.com", "stud-dev", "student", "Emilia Smith", "202100111", 0),
    ("harper.lee", "harper.lee@example.com", "stud-dev", "student", "Harper Lee", "202100112", 0),
    ("benjamin.clarke", "benjamin.clarke@example.com", "stud-dev", "student", "Benjamin Clarke", "202100113", 0),
    ("zoe.thompson", "zoe.thompson@example.com", "stud-dev", "student", "Zoe Thompson", "202100114", 0),
    ("aiden.kim", "aiden.kim@example.com", "stud-dev", "student", "Aiden Kim", "202100115", 0),
    ("isla.murphy", "isla.murphy@example.com", "stud-dev", "student", "Isla Murphy", None, 1),
    ("ravi.desai", "ravi.desai@example.com", "stud-dev", "student", "Ravi Desai", None, 1),
    ("elena.petrov", "elena.petrov@example.com", "stud-dev", "student", "Elena Petrov", None, 1),
]

ADDITIONAL_LANDLORDS = [
    ("chen.properties", "owner@chenproperties.example.com", "land-dev", "landlord", "Mei Chen", None, 0),
    ("harbor.rentals", "owner@harborrentals.example.com", "land-dev", "landlord", "Daniel Harbor", None, 0),
    ("oakwood.estates", "owner@oakwoodestates.example.com", "land-dev", "landlord", "Priya Shah", None, 0),
    ("riverside.homes", "owner@riversidehomes.example.com", "land-dev", "landlord", "Marcus Rivera", None, 0),
    ("campus.keepers", "owner@campuskeepers.example.com", "land-dev", "landlord", "Hannah Brooks", None, 0),
    ("sunrise.leases", "owner@sunriseleases.example.com", "land-dev", "landlord", "Omar Haddad", None, 0),
    ("greenview.rentals", "owner@greenviewrentals.example.com", "land-dev", "landlord", "Sofia Green", None, 0),
    ("westfield.properties", "owner@westfieldproperties.example.com", "land-dev", "landlord", "James West", None, 0),
    ("maple.housing", "owner@maplehousing.example.com", "land-dev", "landlord", "Amina Yusuf", None, 0),
    ("summit.realty", "owner@summitrealty.example.com", "land-dev", "landlord", "Ethan Summit", None, 0),
    ("brighton.estates", "owner@brightonestates.example.com", "land-dev", "landlord", "Chloe Bright", None, 0),
    ("northgate.rentals", "owner@northgaterentals.example.com", "land-dev", "landlord", "Noah North", None, 0),
    ("stonebridge.homes", "owner@stonebridgehomes.example.com", "land-dev", "landlord", "Leila Stone", None, 0),
]

USERS = [*EXISTING_USERS, *ADDITIONAL_STUDENTS, *ADDITIONAL_LANDLORDS]

EXISTING_LISTINGS = [
    (
        "landlord@example.com", "Verified Active Flat", "st_augustine", 3500,
        "wifi,parking", "active", "555-0100", "photo1.jpg", "2099-01-01T10:00:00+00:00",
    ),
    (
        "landlord@example.com", "Pending Unit", "mount_hope", 2800,
        "wifi", "pending", "555-0101", "", "2099-01-02T10:00:00+00:00",
    ),
]

ADDITIONAL_LISTINGS = [
    (
        "landlord2@example.com", "Mount Hope Studio", "mount_hope", 2150,
        "wifi,laundry", "pending", "555-0103", "", "2099-03-01T10:00:00+00:00",
    ),
    (
        "landlord2@example.com", "Garden Flat", "st_augustine", 2900,
        "wifi,parking,balcony", "approved", "555-0104", "photo4.jpg", "2099-03-02T10:00:00+00:00",
    ),
    (
        "owner@chenproperties.example.com", "Campus View Room", "st_augustine", 1850,
        "wifi,desk", "active", "555-0105", "", "2099-03-03T10:00:00+00:00",
    ),
    (
        "owner@chenproperties.example.com", "Quiet Corner Unit", "mount_hope", 2400,
        "wifi,parking", "inactive", "555-0106", "", "2099-03-04T10:00:00+00:00",
    ),
    (
        "owner@harborrentals.example.com", "Harbor Lane Flat", "mount_hope", 3200,
        "wifi,dishwasher,balcony", "active", "555-0107", "photo7.jpg", "2099-03-05T10:00:00+00:00",
    ),
    (
        "owner@harborrentals.example.com", "Harbor Studio", "st_augustine", 2600,
        "wifi,laundry", "rejected", "555-0108", "", "2099-03-06T10:00:00+00:00",
    ),
    (
        "owner@oakwoodestates.example.com", "Oakwood Shared House", "st_augustine", 1950,
        "wifi,laundry,backyard", "active", "555-0109", "", "2099-03-07T10:00:00+00:00",
    ),
    (
        "owner@oakwoodestates.example.com", "Oakwood Loft", "mount_hope", 3450,
        "wifi,parking,gym", "approved", "555-0110", "photo10.jpg", "2099-03-08T10:00:00+00:00",
    ),
    (
        "owner@riversidehomes.example.com", "Riverside One Bedroom", "mount_hope", 3100,
        "wifi,river-view", "active", "555-0111", "", "2099-04-01T10:00:00+00:00",
    ),
    (
        "owner@riversidehomes.example.com", "Riverside Basement Suite", "st_augustine", 1750,
        "wifi,storage", "pending", "555-0112", "", "2099-04-02T10:00:00+00:00",
    ),
    (
        "owner@campuskeepers.example.com", "Keepers Campus Flat", "st_augustine", 2750,
        "wifi,parking", "active", "555-0113", "", "2099-04-03T10:00:00+00:00",
    ),
    (
        "owner@campuskeepers.example.com", "Keepers Annex", "mount_hope", 2250,
        "wifi,laundry", "inactive", "555-0114", "", "2099-04-04T10:00:00+00:00",
    ),
    (
        "owner@sunriseleases.example.com", "Sunrise Studio", "st_augustine", 2050,
        "wifi,bike-storage", "approved", "555-0115", "", "2099-04-05T10:00:00+00:00",
    ),
    (
        "owner@sunriseleases.example.com", "Sunrise Two Bedroom", "mount_hope", 3600,
        "wifi,parking,laundry", "active", "555-0116", "photo16.jpg", "2099-04-06T10:00:00+00:00",
    ),
    (
        "owner@greenviewrentals.example.com", "Greenview Townhouse", "mount_hope", 3800,
        "wifi,garden,parking", "approved", "555-0117", "", "2099-05-01T10:00:00+00:00",
    ),
    (
        "owner@greenviewrentals.example.com", "Greenview Small Room", "st_augustine", 1600,
        "wifi,desk", "rejected", "555-0118", "", "2099-05-02T10:00:00+00:00",
    ),
    (
        "owner@westfieldproperties.example.com", "Westfield Duplex", "st_augustine", 3300,
        "wifi,parking,dishwasher", "active", "555-0119", "photo19.jpg", "2099-05-03T10:00:00+00:00",
    ),
    (
        "owner@westfieldproperties.example.com", "Westfield Attic Room", "mount_hope", 1700,
        "wifi,storage", "pending", "555-0120", "", "2099-05-04T10:00:00+00:00",
    ),
    (
        "owner@maplehousing.example.com", "Maple Court Flat", "st_augustine", 2850,
        "wifi,laundry,parking", "inactive", "555-0121", "", "2099-06-01T10:00:00+00:00",
    ),
    (
        "owner@maplehousing.example.com", "Maple Studio", "mount_hope", 2350,
        "wifi,desk", "inactive", "555-0122", "", "2099-06-02T10:00:00+00:00",
    ),
    (
        "owner@summitrealty.example.com", "Summit Hill Apartment", "mount_hope", 4100,
        "wifi,gym,parking,balcony", "active", "555-0123", "photo23.jpg", "2099-06-03T10:00:00+00:00",
    ),
    (
        "owner@summitrealty.example.com", "Summit Budget Room", "st_augustine", 1800,
        "wifi,shared-kitchen", "rejected", "555-0124", "", "2099-06-04T10:00:00+00:00",
    ),
    (
        "owner@brightonestates.example.com", "Brighton House Room", "st_augustine", 2300,
        "wifi,laundry,backyard", "pending", "555-0125", "", "2099-07-01T10:00:00+00:00",
    ),
    (
        "owner@brightonestates.example.com", "Brighton Loft", "mount_hope", 3500,
        "wifi,parking,balcony", "inactive", "555-0126", "", "2099-07-02T10:00:00+00:00",
    ),
    (
        "owner@northgaterentals.example.com", "Northgate Family Flat", "mount_hope", 3950,
        "wifi,parking,laundry,garden", "active", "555-0127", "photo27.jpg", "2099-07-03T10:00:00+00:00",
    ),
    (
        "owner@northgaterentals.example.com", "Northgate Studio", "st_augustine", 2200,
        "wifi,desk", "pending", "555-0128", "", "2099-07-04T10:00:00+00:00",
    ),
    (
        "owner@stonebridgehomes.example.com", "Stonebridge Ensuite", "st_augustine", 2650,
        "wifi,ensuite,laundry", "approved", "555-0129", "", "2099-07-05T10:00:00+00:00",
    ),
    (
        "owner@stonebridgehomes.example.com", "Stonebridge Garden Flat", "mount_hope", 3050,
        "wifi,parking,garden", "rejected", "555-0130", "", "2099-07-06T10:00:00+00:00",
    ),
]

LISTINGS = [*EXISTING_LISTINGS, *ADDITIONAL_LISTINGS]


def seed(*, reset: bool = False) -> None:
    import app.models  # noqa: F401

    if reset:
        try:
            drop_all()
        except SQLAlchemyError as exc:
            logger.debug("Could not drop tables before seeding: %s", exc)
    ensure_db_and_tables()

    password_hashes = {}
    for _, _, password, *_ in USERS:
        if password not in password_hashes:
            password_hashes[password] = encrypt_password(password)

    with Session(engine) as session:
        existing = session.exec(select(User)).first()
        if existing and not reset:
            return

        for username, email, password, role, name, sid, prov in USERS:
            session.add(
                User(
                    username=username,
                    email=email,
                    password=password_hashes[password],
                    role=role,
                    display_name=name,
                    student_id=sid,
                    provisional=prov,
                )
            )
        session.commit()

        users_by_email = {user.email: user for user in session.exec(select(User)).all()}
        for owner_email, title, area, price, amenities, status, phone, photos, created_at in LISTINGS:
            owner = users_by_email[owner_email]
            session.add(
                Listing(
                    landlord_id=owner.id,
                    title=title,
                    area=area,
                    price=price,
                    amenities=amenities,
                    contact_phone=phone,
                    contact_email=owner_email,
                    photos=photos,
                    created_at=created_at,
                    **fields_for_status(status),
                )
            )
        session.commit()


if __name__ == "__main__":
    seed(reset=True)
    print("Seeded student-accommodation demo data")
