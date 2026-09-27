# Authentication

## Login

Users sign in with email and password on `POST /auth/login`. The password is
checked with `verify_password`, which compares it against the stored hash.

## Session tokens

After a successful sign-in the backend issues a signed session token. The token
carries the user id and an HMAC signature so it cannot be forged.

```python
# Example (this heading-like line is inside a code fence)
# not a heading
token = create_token(user_id)
```

## Registration

New accounts are created with a hashed password; plain passwords are never stored.
