'''Journal Models'''
from __future__ import absolute_import, unicode_literals

import base64
import datetime
import json
import logging
import uuid
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit
import requests

from django.db import models
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.utils.translation import ugettext_lazy as _

from model_utils.models import TimeStampedModel

from jsonfield.fields import JSONField
from slumber.exceptions import HttpClientError, HttpNotFoundError

from wagtail.api import APIField

from wagtail.wagtailadmin.edit_handlers import FieldPanel, StreamFieldPanel
from wagtail.wagtailadmin.navigation import get_explorable_root_page
from wagtail.wagtailimages.edit_handlers import ImageChooserPanel
from wagtail.wagtailcore.fields import RichTextField, StreamField
from wagtail.wagtailcore.models import Collection, CollectionMember, Page
from wagtail.wagtailcore.permission_policies.collections import CollectionOwnershipPermissionPolicy
from wagtail.wagtaildocs.models import AbstractDocument, Document
from wagtail.wagtailimages.models import AbstractImage, AbstractRendition, Image
from wagtail.wagtailsearch import index
from wagtail.wagtailsearch.queryset import SearchableQuerySetMixin

from journals.apps.journals.journal_page_helper import JournalPageMixin
from journals.apps.journals.utils import get_image_url, get_default_expiration_date
from journals.apps.core.models import User
from journals.apps.search.backend import LARGE_TEXT_FIELD_SEARCH_PROPS

logger = logging.getLogger(__name__)


class Organization(models.Model):
    '''Organization Model'''
    name = models.CharField(max_length=255, unique=True)
    site = models.ForeignKey(
        'wagtailcore.Site',
        on_delete=models.CASCADE,
    )

    def __str__(self):
        return self.name


class JournalManager(models.Manager):
    '''Custom model manager for Journals'''

    def create_journal(self, name, org_name, access_length):
        organization = Organization.objects.get(name=org_name)

        if organization:
            journal = self.create(
                name=name, organization=organization, access_length=access_length
            )
            return journal
        else:
            logger.log('Could not find matching organization for %s', org_name)
            return None


class Journal(models.Model):
    """
    A collection of informational articles to which access can be purchased.
    """
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    name = models.CharField(max_length=255)
    video_course_ids = JSONField(
        verbose_name=_('Video Source Course IDs'),
        help_text=_('List of course IDs to pull videos from'),
        null=False,
        blank=False,
        default={'course_runs': []}
    )
    organization = models.ForeignKey(
        'Organization',
        on_delete=models.CASCADE,
        null=False
    )
    access_length = models.IntegerField(null=True, help_text='number of days valid after purchase', default=365)
    objects = JournalManager()

    class Meta(object):
        unique_together = (
            ('name', 'organization')
        )

    def __str__(self):
        return self.name

    @classmethod
    def get_journal_by_id(cls, journal_id):
        """
        Returns the journal object with given id
        if that id is valid else returns None
        """
        try:
            journal = cls.objects.get(id=journal_id)
        except cls.DoesNotExist:
            logger.info("Journal with '{}' id does not exist".format(str(id)))
            journal = None
        return journal


class JournalMetaData(object):
    '''
    JournalMetaData model is used to encapsulate data that is shared with
    discovery-service and ecommerce-service
    NOTE: this is not a database model, just an object wrapper for metadata
    '''
    def __init__(self, journal_about_page, price, currency, sku):
        self.journal_about_page = journal_about_page
        self.journal = journal_about_page.journal
        self.price = price
        self.currency = currency
        self.sku = sku

        # add 10 years to current date, TODO not sure how to pass NULL to
        # ecommerce post
        startDate = datetime.datetime.now()
        endDate = startDate.replace(startDate.year + 10)
        self.expires = str(endDate)

    def get_discovery_data(self):
        '''return data shared with discovery service'''
        return {
            'uuid': str(self.journal.uuid),
            'partner': self.journal.organization.site.siteconfiguration.discovery_partner_id,
            'organization': self.journal.organization.name,
            'title': self.journal.name,
            'price': self.price,
            'currency': self.currency,
            'sku': self.sku,
            'access_length': self.journal.access_length,
            'card_image_url': self.journal_about_page.card_image_absolute_url,
            'short_description': self.journal_about_page.short_description,
            'full_description': self.journal_about_page.long_description,
            'status': 'active' if self.journal_about_page.live else 'inactive',
            'slug': self.journal_about_page.slug,
        }

    def get_ecommerce_data(self):
        '''get ecommerce product data'''
        return {
            'structure': 'standalone',
            'product_class': 'Journal',
            'title': self.journal.name,
            'expires': self.expires,
            'attribute_values': [
                {
                    'name': 'UUID',
                    'code': 'UUID',
                    'value': str(self.journal.uuid)
                }
            ],
            'stockrecords': [{
                'partner': self.journal.organization.site.siteconfiguration.ecommerce_partner_id,
                'partner_sku': self.sku,
                'price_currency': self.currency,
                'price_excl_tax': self.price
            }]
        }

    def __str__(self):
        return self.journal


class JournalAccess(TimeStampedModel):
    """
    Represents a learner's access to a journal.
    """
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    user = models.ForeignKey(User)
    journal = models.ForeignKey(Journal)
    expiration_date = models.DateField()
    order_number = models.CharField(max_length=128, null=True)
    revoked = models.BooleanField(default=False, null=False)
    revoked_date = models.DateField(null=True)

    def __str__(self):
        return str(self.uuid)

    @classmethod
    def get_user_accessible_journal_ids(cls, user):
        """ Finds all journals that user has access to """
        if user.is_anonymous:
            return []
        if user.can_access_admin:
            return Journal.objects.all().values_list('id', flat=True)
        return cls.get_active_access_for_user(user).values_list('journal__id', flat=True)

    @classmethod
    def get_active_access_for_user(cls, user):
        """ Returns all non-revoked, non-expired access grants for the user """
        access_items = cls.objects.filter(
            user=user
        ).filter(
            revoked=False
        ).filter(
            expiration_date__gte=datetime.date.today()
        )
        return access_items

    @classmethod
    def user_has_access(cls, user, journal):
        """ Checks if the user has access to supplied journal """
        if user.can_access_admin:
            return True

        access_items = cls.get_active_access_for_user(user).filter(
            journal=journal
        )

        return True if access_items else False

    @classmethod
    def create_journal_access(cls, user, journal, order_number=None):
        """ Creates new journal access for user """
        expiration_date = datetime.datetime.now() + datetime.timedelta(days=journal.access_length)

        access = cls.objects.create(
            user=user,
            journal=journal,
            expiration_date=expiration_date,
        )
        if order_number:
            access.order_number = order_number

        access.save()
        return access

    @classmethod
    def bulk_create_journal_access(cls, usernames, journal, expiration_date=None):
        """
        Bulk create the JournalAccess on the given parameters

        Args:
            usernames (set): Set of valid usernames.
            journal_id: Journal's id on which we want to give access to users.
            expiration_date:  Journal access' expiration date for all the users.
        """
        journal_access_list = []
        expiration_date = expiration_date if expiration_date else get_default_expiration_date(journal)
        for username in usernames:
            user = User.get_user_by_username(username)
            if user:
                journal_access_list.append(
                    cls(
                        user=user,
                        journal=journal,
                        expiration_date=expiration_date
                    )
                )
        cls.objects.bulk_create(journal_access_list)

    @classmethod
    def revoke_journal_access(cls, order_number):
        """ Revokes access for the access record associated with the given order number """
        access_record = cls.objects.get(order_number=order_number)

        # if access is already revoked it doesn't make sense to revoke access again, and change the revoked date
        if not access_record.revoked:
            access_record.revoked = True
            access_record.revoked_date = datetime.datetime.now()

        access_record.save()
        return access_record


class JournalDocument(AbstractDocument):
    '''
    Override the base Document model so we can index the Document contents for search
    and add reference to JournalAboutPage
    '''
    search_fields = AbstractDocument.search_fields + [
        index.SearchField('data', partial_match=False),
        index.FilterField('id'),
    ]

    admin_form_fields = Document.admin_form_fields

    def data(self):
        '''
        Return the contents of the document as base64 encoded
        data used as input to elasticsearch ingest-attachment plugin
        '''
        self.file.open()
        contents = base64.b64encode(self.file.read()).decode('ascii')
        self.file.close()
        print('in get data for file=', self.file.name)
        return contents


class JournalImage(AbstractImage):
    '''
    Override the base Image model so we can index the Image contents for search
    and add additional fields
    '''
    caption = models.CharField(max_length=1024, blank=True)

    search_fields = AbstractImage.search_fields + [
        index.SearchField('caption', partial_match=True),
        index.FilterField('id'),
    ]

    admin_form_fields = Image.admin_form_fields + (
        'caption',
    )


class JournalImageRendition(AbstractRendition):
    image = models.ForeignKey(JournalImage, related_name='renditions', on_delete=models.CASCADE)

    class Meta:
        unique_together = (
            ('image', 'filter_spec', 'focal_point_key'),
        )


class VideoQuerySet(SearchableQuerySetMixin, models.QuerySet):
    pass


class Video(CollectionMember, index.Indexed, models.Model):
    '''
    Video model
    '''
    block_id = models.CharField(max_length=128, unique=True)
    display_name = models.CharField(max_length=255)
    view_url = models.URLField(max_length=255)
    transcript_url = models.URLField(max_length=255)
    source_course_run = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = VideoQuerySet.as_manager()

    search_fields = CollectionMember.search_fields + [
        index.SearchField('display_name', partial_match=True),
        index.SearchField('transcript', partial_match=False, es_extra=LARGE_TEXT_FIELD_SEARCH_PROPS),
        index.FilterField('id'),
        index.FilterField('source_course_run'),
    ]

    def get_action_url_name(self, action):
        return '%s_%s_modeladmin_%s' % (self._meta.app_label, self._meta.object_name.lower(), action)

    def get_usage(self):
        return JournalPage.objects.filter(videos=self)

    def transcript(self):
        '''
        Read the transcript from the transcript url to provide
        to elasticsearch
        '''
        try:
            response = requests.get(self.transcript_url)  # No auth needed for transcripts
            contents = response.content
            return contents.decode('utf-8') if contents else None
        except Exception as err:  # pylint: disable=broad-except
            print('Exception trying to read transcript', err)
            return None

    def __str__(self):
        return self.display_name


# This has to be below the Video model because XBlockVideoBlock imported below imports the Video model.
# pylint: disable=wrong-import-position
from .blocks import (
    JournalRichTextBlock, JournalImageChooserBlock, JournalRawHTMLBlock, PDFBlock, XBlockVideoBlock,
    PDF_BLOCK_TYPE, VIDEO_BLOCK_TYPE, IMAGE_BLOCK_TYPE, RICH_TEXT_BLOCK_TYPE, RAW_HTML_BLOCK_TYPE,
    STREAM_DATA_DOC_FIELD, STREAM_DATA_TYPE_FIELD)  # noqa


class JournalAboutPage(JournalPageMixin, Page):
    """
    Represents both the base journal with it's metadata and the journal
    marketing page that displays that information.
    """
    journal = models.OneToOneField(Journal, on_delete=models.SET_NULL, null=True, blank=True)
    # title = journal.title ???
    card_image = models.ForeignKey(
        'wagtailimages.Image', on_delete=models.SET_NULL, related_name='+', null=True, blank=True
    )
    hero_image = models.ForeignKey(
        'wagtailimages.Image', on_delete=models.SET_NULL, related_name='+', null=True, blank=True
    )
    short_description = models.CharField(max_length=128, blank=True, default='')
    long_description = models.TextField(blank=True, default=None, null=True)
    custom_content = RichTextField(blank=True)

    content_panels = Page.content_panels + [
        FieldPanel('short_description'),
        FieldPanel('long_description'),
        ImageChooserPanel('card_image'),
        ImageChooserPanel('hero_image'),
        FieldPanel('custom_content'),
    ]

    parent_page_types = ['JournalIndexPage']
    subpage_types = ['JournalPage']

    api_fields = [
        APIField('short_description'),
        APIField('long_description'),
        APIField('card_image_url'),
        APIField('hero_image_url'),
        APIField('custom_content'),
        APIField('structure'),

    ]

    def get_context(self, request, *args, **kwargs):
        # Update context to include only published pages
        context = super(JournalAboutPage, self).get_context(request, args, kwargs)
        context['root_journal_page_url'] = self.root_journal_page_url
        if request.user.is_authenticated():
            context['user_has_access'] = JournalAccess.user_has_access(request.user, self.journal)
        else:
            context['user_has_access'] = False
        discovery_journal_api_client = self.site.siteconfiguration.discovery_journal_api_client
        journal_data = discovery_journal_api_client.journals(self.journal.uuid).get()
        context['journal_data'] = journal_data
        context['buy_button_url'] = self.generate_require_auth_basket_url(journal_data['sku'])
        return context

    def generate_require_auth_basket_url(self, sku):
        basket_url = self.generate_basket_url(sku)
        encoded_basket_url = quote(basket_url)
        return "/require_auth?forward={}".format(encoded_basket_url)

    def generate_basket_url(self, sku):  # pylint: disable=missing-docstring
        ecommerce_base_url = self.site.siteconfiguration.ecommerce_public_url_root
        (scheme, netloc, _, _, _) = urlsplit(ecommerce_base_url)
        basket_url = urlunsplit((
            scheme,
            netloc,
            '/basket/add/',
            'sku={sku}'.format(sku=sku),
            ''
        ))
        return basket_url

    def update_related_objects(self, deactivate=False):  # pylint: disable=missing-docstring

        def update_service(client, data, service_name):
            try:
                client.journals(self.journal.uuid).patch(data)
            except HttpNotFoundError as err:
                # Only a WARN because this will often happen on JournalAboutPage creation.
                logging.warning(
                    "JournalAboutPage unable to update {service_name} because UUID doesn't exist: {error}".format(
                        service_name=service_name,
                        error=err.content
                    )
                )
            except HttpClientError as err:
                logging.error("Error updating {service_name} after JournalAboutPage publish: {error}".format(
                    service_name=service_name,
                    error=err.content
                ))

        discovery_data = {
            "status": "active" if not deactivate else "inactive",
            "card_image_url": self.card_image_absolute_url,
            "title": self.title,
            "full_description": self.long_description,
            "short_description": self.short_description,
            "slug": self.slug
        }

        if self.journal:
            self.journal.name = self.title
            self.journal.save()

        update_service(
            self.site.siteconfiguration.discovery_journal_api_client,
            discovery_data,
            "discovery"
        )

        update_service(
            self.site.siteconfiguration.ecommerce_journal_api_client,
            {'title': self.title},
            'ecommerce'
        )

    @property
    def hero_image_url(self):
        """
        Get the relative url for the hero Image
        """
        if not self.hero_image:
            return ''

        return get_image_url(self.hero_image)

    @property
    def card_image_url(self):
        """
        Get the relative url for the card Image
        """
        if not self.card_image:
            return ''

        return get_image_url(self.card_image)

    @property
    def card_image_absolute_url(self):
        if not self.card_image:
            return ''
        is_absolute_url = bool(urlparse(self.card_image_url).netloc)
        if is_absolute_url:
            return self.card_image_url
        else:
            return urljoin(self.site.root_url, self.card_image_url)

    @property
    def site(self):
        return self.journal.organization.site  # pylint: disable=no-member

    @property
    def root_journal_page_url(self):
        descendants = self.get_descendants()
        return descendants[0].full_url if descendants else '#'

    @property
    def structure(self):
        """ Returns hierarchy of published journal pages as a dict """
        journal_structure = [
            struct for struct in
            (
                journal_page.specific.get_nested_children(live_only=True)
                for journal_page in self.get_children()
                if self.get_descendants().live().count() > 0
            )
            if struct is not None

        ]

        return journal_structure


class JournalIndexPage(JournalPageMixin, Page):
    """
    The marketing page that shows all the journals available on a given site.
    Publicly available.
    """
    subpage_types = ['JournalAboutPage']

    hero_image = models.ForeignKey(
        'wagtailimages.Image', on_delete=models.SET_NULL, related_name='+', null=True, blank=True
    )
    intro = RichTextField(blank=True)

    content_panels = Page.content_panels + [
        ImageChooserPanel('hero_image'),
        FieldPanel('intro', classname="full"),
    ]

    search_fields = Page.search_fields + [
        index.SearchField('intro', partial_match=True),
        index.SearchField('search_description', partial_match=True)
    ]

    api_fields = [
        APIField('intro'),
        APIField('hero_image_url')
    ]

    @property
    def hero_image_url(self):
        if self.hero_image:
            return get_image_url(self.hero_image)
        else:
            return ''

    @property
    def site(self):
        about_page = self.get_first_child()
        if about_page:
            return about_page.specific.site
        else:
            return None


class JournalPage(JournalPageMixin, Page):
    """
    A page inside a journal. These can be nested indefinitely. Restricted to
    users who purchased access to the journal.
    """
    journal_about_page = models.ForeignKey(
        JournalAboutPage,
        null=True,
        blank=True,
        related_name='journal_pages',
        on_delete=models.SET_NULL
    )

    parent_page_types = ['JournalAboutPage', 'JournalPage']
    subpage_types = ['JournalPage']

    body = StreamField([
        (RICH_TEXT_BLOCK_TYPE, JournalRichTextBlock(
            features=['h1', 'h2', 'h3', 'ol', 'ul', 'bold', 'italic', 'link', 'hr', 'document-link', 'image']
        )),
        (RAW_HTML_BLOCK_TYPE, JournalRawHTMLBlock()),
        (IMAGE_BLOCK_TYPE, JournalImageChooserBlock()),
        (PDF_BLOCK_TYPE, PDFBlock()),
        (VIDEO_BLOCK_TYPE, XBlockVideoBlock()),
    ], blank=True)

    images = models.ManyToManyField(JournalImage)
    videos = models.ManyToManyField(Video)
    documents = models.ManyToManyField(JournalDocument)

    content_panels = Page.content_panels + [
        StreamFieldPanel('body')
    ]

    search_fields = Page.search_fields + [
        index.SearchField('body', partial_match=True),
        index.SearchField('search_description', partial_match=True)
    ]

    api_fields = [
        APIField('body'),
        APIField('previous_page_id'),
        APIField('next_page_id'),
    ]

    def update_related_objects(self, clear=False):
        """
        Update the relationship of related objects (docs, videos)
        This gets called when page is published/unpublished
        """
        if not clear:
            new_docs, new_videos, new_images = self._get_related_objects(documents=True, videos=True, images=True)
        else:
            new_docs = set()
            new_videos = set()
            new_images = set()

        self.documents.set(new_docs)  # pylint: disable=no-member
        self.videos.set(new_videos)  # pylint: disable=no-member
        self.images.set(new_images)  # pylint: disable=no-member
        self.journal_about_page = self._calculate_journal_about_page()
        self.save()

    def _get_related_objects(self, documents=True, videos=True, images=True):
        """
        Find set of related objects found in page
        Returns:
        document set(), video set(), image set()
        each containg a list of corresponding objects models
        """
        doc_set = set()
        video_set = set()
        image_set = set()

        for data in self.body.stream_data:  # pylint: disable=no-member
            # TODO: search for images/docs embedded in RichText block as well
            block_type = data.get(STREAM_DATA_TYPE_FIELD, None)
            if documents and block_type == PDF_BLOCK_TYPE:
                doc_set.add(JournalDocument.objects.get(id=data.get('value').get(STREAM_DATA_DOC_FIELD)))
            elif videos and block_type == VIDEO_BLOCK_TYPE:
                video_set.add(Video.objects.get(id=data.get('value').get('video')))
            elif images and block_type == IMAGE_BLOCK_TYPE:
                image_set.add(JournalImage.objects.get(id=data.get('value').get('image')))

        return doc_set, video_set, image_set

    def get_context(self, request, *args, **kwargs):
        context = super(JournalPage, self).get_context(request, args, kwargs)
        context['journal_structure'] = self.get_journal_structure()

        context['prevPage'] = self.get_prev_page()
        context['nextPage'] = self.get_next_page()

        return context

    def get_prev_page(self, live_only=True):
        """
        Get the previous page for navigation. Search order is previous sibling's last descendant,
        previous sibling, then parent
        """
        prev_sib = self.get_prev_siblings().first()
        if prev_sib:
            last_child = prev_sib.specific.get_last_descendant(live_only=live_only)
            return last_child if last_child else prev_sib

        prev_ancestor = self.get_ancestors().last()
        if prev_ancestor and isinstance(prev_ancestor.specific, JournalPage):
            return prev_ancestor if \
                (not live_only or live_only and prev_ancestor.specific.live) else \
                prev_ancestor.specific.get_prev_page(live_only=live_only)
        else:
            return None

    def get_next_page(self, children_and_sibs=True, live_only=True):
        """
        Get the next page for navigation. Search order is child, sibling then
        parent next sibling recursively
        """
        if children_and_sibs:
            next_child = self.get_descendants().live().first() if live_only else self.get_descendants().first()
            if next_child:
                return next_child

            next_sib = self.get_next_sibling()
            if next_sib:
                return next_sib if (not live_only or live_only and next_sib.live) \
                    else next_sib.specific.get_next_page(children_and_sibs=True, live_only=live_only)

        #  no direct children or siblings, now lets recursively check parent's siblings
        parent = self.get_parent()
        if not isinstance(parent.specific, JournalPage):
            return None
        next_sib = parent.get_next_sibling()
        if next_sib:
            return next_sib if (not live_only or live_only and next_sib.live) \
                else next_sib.specific.get_next_page(children_and_sibs=True, live_only=live_only)
        else:
            return parent.specific.get_next_page(children_and_sibs=False, live_only=True)

    @property
    def previous_page_id(self):
        page = self.get_prev_page()
        return page.id if page else None

    @property
    def next_page_id(self):
        page = self.get_next_page()
        return page.id if page else None

    def get_last_descendant(self, live_only=True):
        """
        get the last descendant of this page
        """
        if live_only:
            return self.get_descendants().live().last()
        else:
            return self.get_descendants().last()

    def serve(self, request, *args, **kwargs):
        if not request.user.is_authenticated():
            return HttpResponseRedirect('/login/')
        journal = self.get_journal()
        has_access = JournalAccess.user_has_access(request.user, journal)
        if not has_access:
            raise PermissionDenied
        return super(JournalPage, self).serve(request, args, kwargs)

    @property
    def site(self):
        return self.get_journal().organization.site

    def get_journal(self):
        """ Get journal associated with this page """
        journal_about = self.get_journal_about_page()
        return journal_about.journal

    def get_journal_about_page(self):
        """ Gets the journal about page field and calculates it if null """
        if not self.journal_about_page:
            self.journal_about_page = self._calculate_journal_about_page()
            self.save()
        return self.journal_about_page

    def _calculate_journal_about_page(self):
        """return about_page for journal"""
        journal_about = None
        parent = self.get_parent()
        journal_about = parent.specific
        while True:
            if isinstance(parent.specific, JournalAboutPage):
                journal_about = parent.specific
                break
            try:
                parent = parent.get_parent()
            except:  # noqa pylint: disable=bare-except
                logging.error("Cannot find parent of {}".format(self))
                break
        return journal_about

    def get_journal_structure(self):
        """ Returns the heirarchy of the journal as a dict """
        structure = {
            "journal_structure": self.get_journal_about_page().structure
        }

        return structure

    def get_json_journal_structure(self):
        return json.dumps(self.get_journal_structure())


class WagtailModelManager(object):
    """
    Class to have utility methods for wagtail models
    """

    @staticmethod
    def get_user_pages(user, pages=None):
        """
        Args:
            user: instance of User model
            pages: queryset of pages to filter
        Returns: wagtail pages queryset where given user has add, edit, publish or lock permissions
        if pages queryset is provided filter is applied on that.
        """
        root_page = get_explorable_root_page(user)

        if root_page:
            user_pages = Page.objects.descendant_of(root_page, inclusive=True)
            if pages:
                user_pages = user_pages.filter(pk__in=pages)

        else:
            user_pages = Page.objects.none()

        return user_pages

    @staticmethod
    def get_user_collections(user, collection_type, collections=None):
        """
        Args:
            user: instance of User model
            collection_type: string for filtering collections based on their type
            collections: queryset of collections to filter
        Returns: wagtail collections queryset where given user has add, change permissions
        if collections queryset is provided filter is applied on that.
        """
        model_mapping = {
            'documents': {"model": Document, "owner_field": "uploaded_by_user"},
            'images': {"model": Image, "owner_field": "uploaded_by_user"},
            'videos': {"model": Video, "owner_field": "source_course_run"},
        }
        if not collections:
            collections = Collection.objects.all()

        collection_permission_policy = CollectionOwnershipPermissionPolicy(
            model_mapping[collection_type]["model"], owner_field_name=model_mapping[collection_type]["owner_field"]
        )
        user_collections = collection_permission_policy.collections_user_has_any_permission_for(
            user, ['add', 'change']
        )
        return collections.filter(pk__in=user_collections)

    @staticmethod
    def get_user_images(user):
        """
        Args:
            user: instance of User model
        Returns: wagtail images queryset where given user has add or change permissions

        """
        # inline import to avoid AppRegistryNotReady: Models aren't loaded yet exception
        from wagtail.wagtailimages.permissions import permission_policy as image_permission_policy
        return image_permission_policy.instances_user_has_any_permission_for(
            user, ['add', 'change']
        )

    @staticmethod
    def get_user_documents(user):
        """
        Args:
            user: instance of User model
        Returns: wagtail documents queryset where given user has add or change permissions

        """
        # inline import to avoid AppRegistryNotReady: Models aren't loaded yet exception
        from wagtail.wagtaildocs.permissions import permission_policy as document_permission_policy
        return document_permission_policy.instances_user_has_any_permission_for(
            user, ['add', 'change']
        )


class UserPageVisit(models.Model):
    """
    Record the datetime at which user visited the page.
    """
    user = models.ForeignKey(User, null=True, related_name='visited_pages', on_delete=models.SET_NULL)
    page = models.ForeignKey('wagtailcore.Page', null=True, on_delete=models.SET_NULL)
    visited_at = models.DateTimeField(auto_now=True, db_index=True)
    stale = models.BooleanField(
        default=False,
        help_text=_(
            'Marked the object stale if the published date of visited page is later than visited at.')
    )

    class Meta:
        unique_together = ("user", "page")
