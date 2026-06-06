from sqlalchemy import Column, Integer, String, Float
from ..database import Base


class ExpiryDefault(Base):
    __tablename__ = "expiry_defaults"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String, nullable=False, index=True)
    name_pattern = Column(String, nullable=False)
    storage_type = Column(String, nullable=False)
    default_days = Column(Integer, nullable=False)
    notes = Column(String, nullable=True)
    priority = Column(Integer, default=0)  # higher = checked first
