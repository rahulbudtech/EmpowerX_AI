"""Token ``type`` values issued by EmpowerX Node (HS256 access JWTs)."""

from __future__ import annotations

# User-triggered DICOM submit (POST /radiology/dicom/analyze)
ANALYZE_ALLOWED_TOKEN_TYPES: frozenset[str] = frozenset(
    {
        "technician_access",
        "facility_access",
    }
)

# Any access JWT Node might forward (verify when Authorization is present)
ALL_ACCESS_TOKEN_TYPES: frozenset[str] = frozenset(
    {
        "technician_access",
        "facility_access",
        "specialist_access",
        "admin_access",
        "access",  # legacy
    }
)
