from datetime import date, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

LANG = Literal["bn", "hi", "en"]
RITUAL = Literal[
    "griha", "annaprashan", "lakshmi", "saraswati", "other",
    "satyanarayan", "shiva", "ganesh", "chandi", "tulsi", "navagraha", "homa",
    "mangalchandi", "bipadtarini", "durga", "kali", "jagaddhatri", "janmashtami",
    "shivaratri", "dol", "rath", "manasa", "sheetala", "kartik", "basanti",
    "nabanna", "wedding", "ashirbad", "namkaran", "hatekhori", "upanayan",
    "vastushanti", "bhoomi", "vishwakarma", "halkhata", "shop", "vehicle",
    "shraddha", "tarpan", "pindadaan",
]
AREA = Literal["englishBazar", "oldMalda", "other"]
SLOT = Literal["morning", "afternoon", "evening", "flexible"]
STATUS = Literal["requested", "quoted", "confirmed", "completed", "cancelled"]
Code = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
Amount = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
Revision = Annotated[int, Field(strict=True, ge=0)]


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


class RequestDetails(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    lang: LANG
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(pattern=r"^(\+91)?[6-9][0-9]{9}$")
    ritual: RITUAL
    date: date
    time: SLOT
    area: AREA
    locality: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=300)
    preferred: str = Field(default="", max_length=120)
    customRitual: str | None = Field(default=None, max_length=80)

    @field_validator("phone")
    @classmethod
    def standardize_phone(cls, value: str) -> str:
        return value.removeprefix("+91")

    @model_validator(mode="after")
    def check_ritual(self):
        if self.ritual == "other" and not self.customRitual:
            raise ValueError("Specify the puja when selecting Other")
        if self.ritual != "other":
            self.customRitual = None
        return self


class NewBooking(RequestDetails):
    key: Code


class Quote(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    purohit: str = Field(min_length=1, max_length=120)
    time: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    dakshina: Amount
    travel: Amount
    materials: Amount
    samagri: str = Field(default="", max_length=1500)


class UpdateBooking(BaseModel):
    status: Literal["quoted", "completed", "cancelled"]
    revision: Revision
    quote: Quote | None = None
    purohitId: Code | None = None

    @model_validator(mode="after")
    def check_quote(self):
        if self.status == "quoted" and self.quote is None:
            raise ValueError("A quote is required")
        return self


class AcceptQuote(BaseModel):
    revision: Revision


class BookingId(BaseModel):
    id: Code


class PublicBooking(BaseModel):
    ritual: RITUAL
    customRitual: str | None = None
    date: date
    status: STATUS
    revision: Revision
    quote: Quote | None


class AdminUpdateResult(PublicBooking):
    purohitId: Code | None = None


class AdminBooking(AdminUpdateResult, RequestDetails):
    id: Code
    createdAt: str


class BookingPage(BaseModel):
    bookings: list[AdminBooking]
    next: int | None
