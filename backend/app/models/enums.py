import enum


class VolumeTier(str, enum.Enum):
    under_500k = "under_500k"
    tier_500k_1m = "500k_1m"
    tier_1m_3m = "1m_3m"
    over_3m = "over_3m"


class InvoiceSource(str, enum.Enum):
    upload = "upload"
    email = "email"
    photo = "photo"


class InvoiceStatus(str, enum.Enum):
    received = "received"
    rendering = "rendering"
    extracting = "extracting"
    extracted = "extracted"
    needs_review = "needs_review"
    confirmed = "confirmed"
    failed = "failed"


class BaseUom(str, enum.Enum):
    lb = "lb"
    oz = "oz"
    gal = "gal"
    fl_oz = "fl_oz"
    each = "each"
    dozen = "dozen"


class ReviewStatus(str, enum.Enum):
    auto = "auto"
    pending = "pending"
    confirmed = "confirmed"
    corrected = "corrected"


class AlertType(str, enum.Enum):
    creep = "creep"
    off_contract = "off_contract"
    above_peer = "above_peer"


class AlertStatus(str, enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"
    dismissed = "dismissed"
