"""Keep OAuth callback queries out of Uvicorn access logs."""
import logging


class AuthQueryRedactionFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].split('?', 1)[0].startswith('/auth/'):
                args[2] = args[2].split('?', 1)[0]
                record.args = tuple(args)
        return True


def configure_auth_logging():
    logger = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, AuthQueryRedactionFilter) for f in logger.filters):
        logger.addFilter(AuthQueryRedactionFilter())
