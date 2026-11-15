"""GET /v1/providers: every registered provider with availability, policy verdict and reasons."""
from ninja import Router

from ..providers import registry

router = Router()


@router.get('/v1/providers', auth=None)
def providers(request, probe: bool = False):
    # probe=true performs advisory network checks (cached 10 minutes); the default makes no network calls.
    # Only an established studio session may trigger those outbound requests.
    return registry.status(probe=probe and bool(request.session.get('studio_owner')))
