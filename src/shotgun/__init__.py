"""shotgun — automated job discovery, tailoring, and application tracking."""

__version__ = "0.1.0"


def _use_system_trust_store() -> None:
    """Verify TLS against the OS trust store instead of certifi's bundle.

    Managed machines often sit behind a TLS-intercepting proxy whose root CA
    is in the OS keychain but not in certifi, so every certifi-based client (httpx,
    requests, the Anthropic SDK) fails with CERTIFICATE_VERIFY_FAILED while
    curl succeeds. Patching ssl at import fixes all of them at once, and is a
    no-op where no such proxy is present.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 — never let trust setup break startup
        pass


_use_system_trust_store()
