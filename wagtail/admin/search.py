from django.forms import Media, MediaDefiningClass
from django.forms.utils import flatatt
from django.template.loader import render_to_string
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from wagtail import hooks
from wagtail.admin.forms.search import SearchForm


class SearchArea(metaclass=MediaDefiningClass):
    template = "wagtailadmin/shared/search_area.html"

    def __init__(
        self,
        label,
        url,
        name=None,
        classname="",
        icon_name="",
        attrs=None,
        order=1000,
    ):
        self.label = label
        self.url = url
        self.classname = classname
        self.icon_name = icon_name
        self.name = name or slugify(str(label))
        self.order = order

        if attrs:
            self.attr_string = flatatt(attrs)
        else:
            self.attr_string = ""

    def __lt__(self, other):
        if not isinstance(other, SearchArea):
            return NotImplemented
        return (self.order, self.label) < (other.order, other.label)

    def __le__(self, other):
        if not isinstance(other, SearchArea):
            return NotImplemented
        return (self.order, self.label) <= (other.order, other.label)

    def __gt__(self, other):
        if not isinstance(other, SearchArea):
            return NotImplemented
        return (self.order, self.label) > (other.order, other.label)

    def __ge__(self, other):
        if not isinstance(other, SearchArea):
            return NotImplemented
        return (self.order, self.label) >= (other.order, other.label)

    def __eq__(self, other):
        if not isinstance(other, SearchArea):
            return NotImplemented
        return (self.order, self.label) == (other.order, other.label)

    def is_shown(self, request):
        """
        Whether this search area should be shown for the given request; permission
        checks etc should go here. By default, search areas are shown all the time
        """
        return True

    def is_active(self, request, current=None):
        if current is None:
            return request.path.startswith(self.url)
        else:
            return self.name == current

    def render_html(self, request, query, current=None):
        return render_to_string(
            self.template,
            {
                "name": self.name,
                "url": self.url,
                "classname": self.classname,
                "icon_name": self.icon_name,
                "attr_string": self.attr_string,
                "label": self.label,
                "active": self.is_active(request, current),
                "query_string": query,
            },
            request=request,
        )


class Search:
    def __init__(self, register_hook_name, construct_hook_name=None):
        self.register_hook_name = register_hook_name
        self.construct_hook_name = construct_hook_name

    @cached_property
    def registered_search_areas(self):
        return sorted([fn() for fn in hooks.get_hooks(self.register_hook_name)])

    def search_items_for_request(self, request):
        return [item for item in self.registered_search_areas if item.is_shown(request)]

    def active_search(self, request, current=None):
        return [
            item
            for item in self.search_items_for_request(request)
            if item.is_active(request, current)
        ]

    @property
    def media(self):
        media = Media()
        for item in self.registered_search_areas:
            media += item.media
        return media

    def render_html(self, request, current=None):
        search_areas = self.search_items_for_request(request)

        # Get query parameter
        form = SearchForm(request.GET)
        query = ""
        if form.is_valid():
            query = form.cleaned_data["q"]

        # provide a hook for modifying the search area, if construct_hook_name has been set
        if self.construct_hook_name:
            for fn in hooks.get_hooks(self.construct_hook_name):
                fn(request, search_areas)

        rendered_search_areas = []
        for item in search_areas:
            rendered_search_areas.append(item.render_html(request, query, current))

        return mark_safe("".join(rendered_search_areas))


admin_search_areas = Search(
    register_hook_name="register_admin_search_area",
    construct_hook_name="construct_search",
)
