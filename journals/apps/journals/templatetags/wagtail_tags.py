"""
This module has those template tags/filters which we use in Wagtail template overrides.
"""
from django import template

from journals.apps.journals.models import WagtailModelManager
from journals.apps.journals.utils import make_md5_hash


register = template.Library()


@register.filter(name='with_permissions')
def collections_user_has_access(collections, request):
    """
    Args:
        collections: queryset of collection objects
        request: http request object

    Returns: queryset of collection objects striping those where user does not have add or change permissions

    """
    return WagtailModelManager.get_user_collections(request.user, collections)


@register.filter(name='md5hash')
def md5hash(value):
    return make_md5_hash(value)
