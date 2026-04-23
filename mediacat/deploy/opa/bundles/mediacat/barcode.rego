# mediacat/barcode.rego — derive country from EAN-13 barcode prefix.

package mediacat

import rego.v1

# Barcode country decode: overrides the default `decode` when a barcode
# is present and its prefix matches a known range.

decode := result if {
    input.fields.barcode
    count(input.fields.barcode) >= 3
    prefix := substring(input.fields.barcode, 0, 3)
    country := barcode_country(prefix)
    country != ""
    result := {
        "status": "matched",
        "decoded": {"country_from_barcode": country},
        "warnings": [],
        "rule_ids": ["barcode_country"],
        "confidence": 0.9,
    }
}

# Lookup function: returns country alpha-2 or "" if unknown.
barcode_country(prefix) := country if {
    some range in data.mediacat.barcode_ranges
    to_number(prefix) >= range.low
    to_number(prefix) <= range.high
    country := range.country
} else := ""
