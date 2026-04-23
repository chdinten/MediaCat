# mediacat/main.rego — entry point for the MediaCat country-decode policy.
#
# This is a skeleton; full decode rules are added in Section 6.
# The application calls POST /v1/data/mediacat/decode with a JSON body.

package mediacat

import rego.v1

# Default response: unknown / pass-through.
default decode := {
    "status": "unknown",
    "decoded": {},
    "warnings": ["no matching rule for this input"],
}

# Decode endpoint: the app sends { "media_format": "vinyl"|"cd", "fields": {...} }.
# Rules in country-specific .rego files override this default when they match.
