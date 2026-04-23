# mediacat/main_test.rego — test the default decode response.

package mediacat_test

import rego.v1

import data.mediacat

test_default_decode_returns_unknown if {
    result := mediacat.decode with input as {
        "media_format": "vinyl",
        "fields": {},
    }
    result.status == "unknown"
}
