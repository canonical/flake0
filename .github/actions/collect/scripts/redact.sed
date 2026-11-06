# flake0-collect: best-effort secret patterns, applied by redact.sh with
# "sed -E". The exact-value layer in redact.sh (FLAKE0_REDACT_VALUES) is the
# reliable one; these only catch common formats and WILL miss custom, encoded or
# split secrets. Needs GNU sed (the I flag, the c\ command). Keep each pattern
# conservative so it does not eat legitimate log content.

# key=value / key: value for common secret-bearing keys. The value runs to the
# next whitespace, quote or separator, so surrounding structure is preserved.
# Auth headers are handled by the scheme rule below, so the scheme word (Bearer,
# Basic) is not itself mistaken for the secret.
s/((pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key)[[:space:]]*[=:][[:space:]]*)[^[:space:],;"']+/\1***REDACTED***/Ig

# HTTP auth schemes followed by a credential
s/((bearer|basic)[[:space:]]+)[A-Za-z0-9._~+\/-]+=*/\1***REDACTED***/Ig

# JSON Web Tokens (header.payload.signature)
s/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/***REDACTED-JWT***/g

# AWS access key IDs
s/(AKIA|ASIA)[0-9A-Z]{16}/***REDACTED-AWS-KEY***/g

# PEM private key blocks, including the base64 body (multi-line)
/-----BEGIN ([A-Z]+ )?PRIVATE KEY-----/,/-----END ([A-Z]+ )?PRIVATE KEY-----/c\
***REDACTED-PRIVATE-KEY-BLOCK***
