"""Default example views"""
from gerbi import settings
from gerbi.models import Page, PageAlias
from gerbi.http import get_language_from_request, remove_slug
from gerbi.urlconf_registry import get_urlconf

from django.http import Http404, HttpResponsePermanentRedirect
from django.contrib.sitemaps import Sitemap
from django.core.urlresolvers import resolve, Resolver404
from django.utils import translation
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.core.xheaders import populate_xheaders

LANGUAGE_KEYS = [key for (key, value) in settings.GERBI_LANGUAGES]


class Details(object):
    """
    This class based view get the root pages for navigation
    and the current page to display if there is any.

    All is rendered with the current page's template.
    """

    def __call__(self, request, path=None, lang=None, delegation=True,
            **kwargs):

        if path is None:
            raise ValueError(
                "gerbi.views.Details class view requires the path argument. "
                "Check your urls.py file.")

        # for the ones that might have forgotten to pass the language
        # the language is now removed from the page path
        if settings.GERBI_USE_LANGUAGE_PREFIX and lang is None:
            maybe_lang = path.split("/")[0]
            if maybe_lang in LANGUAGE_KEYS:
                lang = maybe_lang
                path = path[(len(lang) + 1):]

        lang = self.choose_language(lang, request)
        pages_navigation = self.get_navigation(request, path, lang)

        context = {
            'path': path,
            'pages_navigation': pages_navigation,
            'lang': lang,
        }

        is_staff = self.is_user_staff(request)

        gerbi_current_page = self.resolve_page(request, context, is_staff)

        # if no page has been found, we will try to find it via an Alias
        if not gerbi_current_page:
            redirection = self.resolve_alias(request, path, lang)
            if redirection:
                return redirection
        else:
            context['gerbi_current_page'] = gerbi_current_page

        # If unauthorized to see the pages, raise a 404, That can
        # happen with expired pages.
        if not is_staff and not gerbi_current_page.visible:
            raise Http404

        redirection = self.resolve_redirection(request, context)
        if redirection:
            return redirection

        template_name = self.get_template(request, context)

        # could change the current_page
        self.extra_context(request, context)
        gerbi_current_page = context['gerbi_current_page']

        if delegation and gerbi_current_page.delegate_to:
            answer = self.delegate(request, context, delegation, **kwargs)
            if answer:
                return answer

        # if present, the page is not rendered
        if kwargs.get('only_context', False):
            return context
        template_name = kwargs.get('template_name', template_name)
        response = render_to_response(template_name,
            RequestContext(request, context))
        populate_xheaders(request, response, Page, gerbi_current_page.id)
        return response

    def resolve_page(self, request, context, is_staff):
        """Return the appropriate page according to the path."""
        path = context['path']
        lang = context['lang']
        page = Page.objects.from_path(path, lang,
            exclude_drafts=(not is_staff))
        if page:
            return page
        # if the complete path didn't worked out properly
        # and if didn't used GERBI_USE_STRICT_URL setting we gonna
        # try to see if it might be a delegation page.
        # To do that we remove the right part of the url and try again
        # to find a page that match
        if not settings.GERBI_USE_STRICT_URL:
            path = remove_slug(path)
            while path is not None:
                page = Page.objects.from_path(path, lang,
                    exclude_drafts=(not is_staff))
                # find a match. Is the page delegating?
                if page:
                    if page.delegate_to:
                        return page
                path = remove_slug(path)

        return None

    def resolve_alias(self, request, path, lang):
        alias = PageAlias.objects.from_path(request, path, lang)
        if alias:
            url = alias.page.get_url_path(lang)
            return HttpResponsePermanentRedirect(url)
        raise Http404

    def resolve_redirection(self, request, context):
        """Check for redirections."""
        gerbi_current_page = context['gerbi_current_page']
        lang = context['lang']
        if gerbi_current_page.redirect_to_url:
            return HttpResponsePermanentRedirect(gerbi_current_page.redirect_to_url)

        if gerbi_current_page.redirect_to:
            return HttpResponsePermanentRedirect(
                gerbi_current_page.redirect_to.get_url_path(lang))

    def get_navigation(self, request, path, lang):
        """Get the pages that are at the root level."""
        return Page.objects.navigation().order_by("tree_id")

    def choose_language(self, lang, request):
        """Deal with the multiple corner case of choosing the language."""

        # Can be an empty string or None
        if not lang:
            lang = get_language_from_request(request)

        # Raise a 404 if the language is not in not in the list
        if lang not in [key for (key, value) in settings.GERBI_LANGUAGES]:
            raise Http404

        # We're going to serve CMS gerbi in language lang;
        # make django gettext use that language too
        if lang and translation.check_for_language(lang):
            translation.activate(lang)

        return lang

    def get_template(self, request, context):
        """Just there in case you have special business logic."""
        return context['gerbi_current_page'].get_template()

    def is_user_staff(self, request):
        """Return True if the user is staff."""
        return request.user.is_authenticated() and request.user.is_staff

    def extra_context(self, request, context):
        """Call the GERBI_EXTRA_CONTEXT function if there is one."""
        if settings.GERBI_EXTRA_CONTEXT:
            context.update(settings.GERBI_EXTRA_CONTEXT())

    def delegate(self, request, context, delegation=True):
        # if there is a delegation to another view,
        # call this view instead.
        gerbi_current_page = context['gerbi_current_page']
        path = context['path']
        delegate_path = path.replace(
            gerbi_current_page.get_complete_slug(hideroot=False), "")
        # it seems that the urlconf path have to start with a slash
        if len(delegate_path) == 0:
            delegate_path = "/"
        if delegate_path.startswith("//"):
            delegate_path = delegate_path[1:]
        urlconf = get_urlconf(gerbi_current_page.delegate_to)
        try:
            result = resolve(delegate_path, urlconf)
        except Resolver404:
            raise Http404
        if result:
            view, args, kwargs = result
            kwargs.update(context)
            # for now the view is called as is.
            return view(request, *args, **kwargs)


# The view view instance. It's the same object for
# everybody so be careful to maintain it thread safe.
# ie: NO self.attrbitute = something
details = Details()


class PageSitemap(Sitemap):
    """This site map implementation expose the pages
    in the default language only."""
    changefreq = "weekly"
    priority = 0.5

    def items(self):
        return Page.objects.published()

    def lastmod(self, obj):
        return obj.last_modification_date


class PageItemProxy(object):

    def __init__(self, page, lang):
        self.page = page
        self.lang = lang

    def get_absolute_url(self):
        return self.page.get_absolute_url(language=self.lang)


class MultiLanguagePageSitemap(Sitemap):
    """This site map implementation expose the pages
    in all the languages."""
    changefreq = "weekly"
    priority = 0.5

    def items(self):
        item_list = []
        for page in Page.objects.published():
            for lang in page.get_languages():
                item_list.append(PageItemProxy(page, lang))
        return item_list

    def lastmod(self, obj):
        return obj.page.last_modification_date
