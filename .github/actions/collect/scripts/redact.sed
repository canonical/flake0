# flake0-collect: best-effort secret patterns, applied by redact.sh with
# "sed -E". The exact-value layer in redact.sh (FLAKE0_REDACT_VALUES) is the
# reliable one; these only catch common formats and WILL miss custom, encoded or
# split secrets. Needs GNU sed (the I flag, the c\ command). Keep each pattern
# conservative so it does not eat legitimate log content.

# HTTP auth schemes followed by a credential. Before the key rule, which would stop
# at the scheme word in "token: Bearer abc". \b keeps test_basic FAILED readable.
# Not delimited by "/", because a \/ in a bracket also matches a backslash.
s#\b((bearer|basic)[[:space:]]+)[A-Za-z0-9._~+/-]+=*#\1***REDACTED***#Ig

# key=value / key: value for common secret-bearing keys, in plain, JSON and Python
# dict form, and JSON inside a JSON string. The value runs to the next whitespace,
# quote or separator, so surrounding structure is preserved. A backslash before a
# quote ends it too, and any other backslash pair stays whole, so a marker line
# stays valid JSON. A value cannot start with ":", so TestPassword::test_x survives.
s/((pass(word|wd)?|secret([_-]?key)?|token|api[_-]?key|access[_-]?key)\\?["']?[[:space:]]*[=:][[:space:]]*(\\?["'])?)([^[:space:],;"':\\]|\\[^"'])([^[:space:],;"'\\]|\\[^"'])*/\1***REDACTED***/Ig

# Credentials in a URL, like a database relation's postgres://user:pass@host
s#(://[^:/@[:space:]]+:)[^@/"'[:space:]]+@#\1***REDACTED***@#g

# JSON Web Tokens (header.payload.signature)
s/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/***REDACTED-JWT***/g

# AWS access key IDs
s/(AKIA|ASIA)[0-9A-Z]{16}/***REDACTED-AWS-KEY***/g

# A PEM private key on one line, as in JSON. The range below would never see its
# END and would delete the rest of the file.
s/-----BEGIN ([A-Z]+ )?PRIVATE KEY-----.*-----END ([A-Z]+ )?PRIVATE KEY-----/***REDACTED-PRIVATE-KEY-BLOCK***/g

# PEM private key blocks, including the base64 body (multi-line)
/-----BEGIN ([A-Z]+ )?PRIVATE KEY-----/,/-----END ([A-Z]+ )?PRIVATE KEY-----/c\
***REDACTED-PRIVATE-KEY-BLOCK***
