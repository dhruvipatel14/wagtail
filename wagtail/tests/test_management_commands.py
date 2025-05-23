from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import management
from django.core.cache import cache
from django.db import models
from django.test import TestCase, override_settings
from django.utils import timezone

from wagtail.embeds.models import Embed
from wagtail.models import (
    Collection,
    Page,
    PageLogEntry,
    Revision,
    Task,
    Workflow,
    WorkflowTask,
)
from wagtail.signals import page_published, page_unpublished, published, unpublished
from wagtail.test.testapp.models import (
    DraftStateModel,
    EventPage,
    FullFeaturedSnippet,
    PurgeRevisionsProtectedTestModel,
    SecretPage,
    SimplePage,
)
from wagtail.test.utils import WagtailTestUtils


class TestFixTreeCommand(TestCase):
    fixtures = ["test.json"]

    def badly_delete_page(self, page):
        # Deletes a page the wrong way.
        # This will not update numchild and may leave orphans
        models.Model.delete(page)

    def run_command(self, **options):
        options.setdefault("interactive", False)

        output = StringIO()
        management.call_command("fixtree", stdout=output, **options)
        output.seek(0)

        return output

    def test_fixes_numchild(self):
        # Get homepage and save old value
        homepage = Page.objects.get(url_path="/home/")
        old_numchild = homepage.numchild

        # Break it
        homepage.numchild = 12345
        homepage.save()

        # Check that its broken
        self.assertEqual(Page.objects.get(url_path="/home/").numchild, 12345)

        # Call command
        self.run_command()

        # Check if its fixed
        self.assertEqual(Page.objects.get(url_path="/home/").numchild, old_numchild)

    def test_fixes_depth(self):
        # Get homepage and save old value
        homepage = Page.objects.get(url_path="/home/")
        old_depth = homepage.depth

        # Break it
        homepage.depth = 12345
        homepage.save()

        # also break the root collection's depth
        root_collection = Collection.get_first_root_node()
        root_collection.depth = 42
        root_collection.save()

        # Check that its broken
        self.assertEqual(Page.objects.get(url_path="/home/").depth, 12345)
        self.assertEqual(Collection.objects.get(id=root_collection.id).depth, 42)

        # Call command
        self.run_command()

        # Check if its fixed
        self.assertEqual(Page.objects.get(url_path="/home/").depth, old_depth)
        self.assertEqual(Collection.objects.get(id=root_collection.id).depth, 1)

    def test_detects_orphans(self):
        events_index = Page.objects.get(url_path="/home/events/")
        christmas_page = EventPage.objects.get(url_path="/home/events/christmas/")

        # Delete the events index badly
        self.badly_delete_page(events_index)

        # Check that christmas_page is still in the tree
        self.assertTrue(Page.objects.filter(id=christmas_page.id).exists())

        # Call command
        output = self.run_command()

        # Check that the issues were detected
        output_string = output.read()
        self.assertIn("Incorrect numchild value found for pages: [2]", output_string)
        # Note that page ID 15 was also deleted, but is not picked up here, as
        # it is a child of 14.
        self.assertIn("Orphaned pages found: [4, 5, 6, 9, 13, 15]", output_string)

        # Check that christmas_page is still in the tree
        self.assertTrue(Page.objects.filter(id=christmas_page.id).exists())

    def test_deletes_orphans(self):
        events_index = Page.objects.get(url_path="/home/events/")
        christmas_page = EventPage.objects.get(url_path="/home/events/christmas/")

        # Delete the events index badly
        self.badly_delete_page(events_index)

        # Check that christmas_page is still in the tree
        self.assertTrue(Page.objects.filter(id=christmas_page.id).exists())

        # Call command
        # delete_orphans simulates a user pressing "y" at the prompt
        output = self.run_command(delete_orphans=True)

        # Check that the issues were detected
        output_string = output.read()
        self.assertIn("Incorrect numchild value found for pages: [2]", output_string)
        self.assertIn("7 orphaned pages deleted.", output_string)

        # Check that christmas_page has been deleted
        self.assertFalse(Page.objects.filter(id=christmas_page.id).exists())

    def test_remove_path_holes(self):
        events_index = Page.objects.get(url_path="/home/events/")
        # Delete the event page in path position 0001
        Page.objects.get(path=events_index.path + "0001").delete()

        self.run_command(full=True)
        # the gap at position 0001 should have been closed
        events_index = Page.objects.get(url_path="/home/events/")
        self.assertTrue(Page.objects.filter(path=events_index.path + "0001").exists())


class TestMovePagesCommand(TestCase):
    fixtures = ["test.json"]

    def run_command(self, from_, to):
        management.call_command("move_pages", str(from_), str(to), stdout=StringIO())

    def test_move_pages(self):
        # Get pages
        events_index = Page.objects.get(url_path="/home/events/")
        about_us = Page.objects.get(url_path="/home/about-us/")
        page_ids = events_index.get_children().values_list("id", flat=True)

        # Move all events into "about us"
        self.run_command(events_index.id, about_us.id)

        # Check that all pages moved
        for page_id in page_ids:
            self.assertEqual(Page.objects.get(id=page_id).get_parent(), about_us)


class TestSetUrlPathsCommand(TestCase):
    fixtures = ["test.json"]

    def run_command(self):
        management.call_command("set_url_paths", stdout=StringIO())

    def test_set_url_paths(self):
        self.run_command()


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        },
    }
)
class TestPublishScheduledPagesCommand(WagtailTestUtils, TestCase):
    fixtures = ["test.json"]

    def setUp(self):
        cache.clear()

        # Find root page
        self.root_page = Page.objects.get(id=2)

    def test_go_live_page_will_be_published(self):
        # Connect a mock signal handler to page_published signal
        signal_fired = [False]
        signal_page = [None]

        def page_published_handler(sender, instance, **kwargs):
            signal_fired[0] = True
            signal_page[0] = instance

        page_published.connect(page_published_handler)

        try:
            page = SimplePage(
                title="Hello world!",
                slug="hello-world",
                content="hello",
                live=False,
                has_unpublished_changes=True,
                go_live_at=timezone.now() - timedelta(days=1),
            )
            self.root_page.add_child(instance=page)

            page.save_revision(approved_go_live_at=timezone.now() - timedelta(days=1))

            p = Page.objects.get(slug="hello-world")
            self.assertFalse(p.live)
            self.assertTrue(
                Revision.page_revisions.filter(object_id=p.id)
                .exclude(approved_go_live_at__isnull=True)
                .exists()
            )
            with self.assertNumQueries(49):
                with self.captureOnCommitCallbacks(execute=True):
                    management.call_command("publish_scheduled_pages")

            p = Page.objects.get(slug="hello-world")
            self.assertTrue(p.live)
            self.assertTrue(p.first_published_at)
            self.assertFalse(p.has_unpublished_changes)
            self.assertFalse(
                Revision.page_revisions.filter(object_id=p.id)
                .exclude(approved_go_live_at__isnull=True)
                .exists()
            )

            # Check that the page_published signal was fired
            self.assertTrue(signal_fired[0])
            self.assertEqual(signal_page[0], page)
            self.assertEqual(signal_page[0], signal_page[0].specific)
        finally:
            page_published.disconnect(page_published_handler)

    def test_go_live_page_created_by_editor_will_be_published(self):
        # Connect a mock signal handler to page_published signal
        signal_fired = [False]
        signal_page = [None]

        editor = self.create_user("ed")
        editor.groups.add(Group.objects.get(name="Site-wide editors"))

        def page_published_handler(sender, instance, **kwargs):
            signal_fired[0] = True
            signal_page[0] = instance

        page_published.connect(page_published_handler)

        try:
            page = SimplePage(
                title="Hello world!",
                slug="hello-world",
                content="hello",
                live=False,
                has_unpublished_changes=True,
                go_live_at=timezone.now() - timedelta(days=1),
            )
            self.root_page.add_child(instance=page)

            page.save_revision(
                user=editor, approved_go_live_at=timezone.now() - timedelta(days=1)
            )

            p = Page.objects.get(slug="hello-world")
            self.assertFalse(p.live)
            self.assertTrue(
                Revision.page_revisions.filter(object_id=p.id)
                .exclude(approved_go_live_at__isnull=True)
                .exists()
            )

            with self.assertNumQueries(49):
                with self.captureOnCommitCallbacks(execute=True):
                    management.call_command("publish_scheduled_pages")

            p = Page.objects.get(slug="hello-world")
            self.assertTrue(p.live)
            self.assertTrue(p.first_published_at)
            self.assertFalse(p.has_unpublished_changes)
            self.assertFalse(
                Revision.page_revisions.filter(object_id=p.id)
                .exclude(approved_go_live_at__isnull=True)
                .exists()
            )

            # Check that the page_published signal was fired
            self.assertTrue(signal_fired[0])
            self.assertEqual(signal_page[0], page)
            self.assertEqual(signal_page[0], signal_page[0].specific)
        finally:
            page_published.disconnect(page_published_handler)

    def test_go_live_when_newer_revision_exists(self):
        page = SimplePage(
            title="Hello world!",
            slug="hello-world",
            content="hello",
            live=False,
            has_unpublished_changes=True,
            go_live_at=timezone.now() - timedelta(days=1),
        )
        self.root_page.add_child(instance=page)

        page.save_revision(approved_go_live_at=timezone.now() - timedelta(days=1))

        page.title = "Goodbye world!"
        page.save_revision()

        with self.assertNumQueries(49):
            with self.captureOnCommitCallbacks(execute=True):
                management.call_command("publish_scheduled_pages")

        p = Page.objects.get(slug="hello-world")
        self.assertTrue(p.live)
        self.assertTrue(p.has_unpublished_changes)
        self.assertEqual(p.title, "Hello world!")

    def test_future_go_live_page_will_not_be_published(self):
        page = SimplePage(
            title="Hello world!",
            slug="hello-world",
            content="hello",
            live=False,
            go_live_at=timezone.now() + timedelta(days=1),
        )
        self.root_page.add_child(instance=page)

        page.save_revision(approved_go_live_at=timezone.now() - timedelta(days=1))

        p = Page.objects.get(slug="hello-world")
        self.assertFalse(p.live)
        self.assertTrue(
            Revision.page_revisions.filter(object_id=p.id)
            .exclude(approved_go_live_at__isnull=True)
            .exists()
        )

        with self.assertNumQueries(42):
            with self.captureOnCommitCallbacks(execute=True):
                management.call_command("publish_scheduled_pages")

        p = Page.objects.get(slug="hello-world")
        self.assertFalse(p.live)
        self.assertTrue(
            Revision.page_revisions.filter(object_id=p.id)
            .exclude(approved_go_live_at__isnull=True)
            .exists()
        )

    def test_expired_page_will_be_unpublished(self):
        # Connect a mock signal handler to page_unpublished signal
        signal_fired = [False]
        signal_page = [None]

        def page_unpublished_handler(sender, instance, **kwargs):
            signal_fired[0] = True
            signal_page[0] = instance

        page_unpublished.connect(page_unpublished_handler)

        try:
            page = SimplePage(
                title="Hello world!",
                slug="hello-world",
                content="hello",
                live=True,
                has_unpublished_changes=False,
                expire_at=timezone.now() - timedelta(days=1),
            )
            self.root_page.add_child(instance=page)

            p = Page.objects.get(slug="hello-world")
            self.assertTrue(p.live)

            with self.assertNumQueries(29):
                with self.captureOnCommitCallbacks(execute=True):
                    management.call_command("publish_scheduled_pages")

            p = Page.objects.get(slug="hello-world")
            self.assertFalse(p.live)
            self.assertTrue(p.has_unpublished_changes)
            self.assertTrue(p.expired)

            # Check that the page_published signal was fired
            self.assertTrue(signal_fired[0])
            self.assertEqual(signal_page[0], page)
            self.assertEqual(signal_page[0], signal_page[0].specific)
        finally:
            page_unpublished.disconnect(page_unpublished_handler)

    def test_future_expired_page_will_not_be_unpublished(self):
        page = SimplePage(
            title="Hello world!",
            slug="hello-world",
            content="hello",
            live=True,
            expire_at=timezone.now() + timedelta(days=1),
        )
        self.root_page.add_child(instance=page)

        p = Page.objects.get(slug="hello-world")
        self.assertTrue(p.live)

        with self.assertNumQueries(6):
            with self.captureOnCommitCallbacks(execute=True):
                management.call_command("publish_scheduled_pages")

        p = Page.objects.get(slug="hello-world")
        self.assertTrue(p.live)
        self.assertFalse(p.expired)


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        },
    }
)
class TestPublishScheduledCommand(WagtailTestUtils, TestCase):
    fixtures = ["test.json"]

    def setUp(self):
        cache.clear()
        self.snippet = DraftStateModel.objects.create(text="Hello world!", live=False)

    def test_go_live_will_be_published(self):
        # Connect a mock signal handler to published signal
        signal_fired = [False]
        signal_obj = [None]

        def published_handler(sender, instance, **kwargs):
            signal_fired[0] = True
            signal_obj[0] = instance

        published.connect(published_handler)

        try:
            go_live_at = timezone.now() - timedelta(days=1)
            self.snippet.has_unpublished_changes = True
            self.snippet.go_live_at = go_live_at

            self.snippet.save_revision(approved_go_live_at=go_live_at)

            self.snippet.refresh_from_db()
            self.assertFalse(self.snippet.live)
            self.assertTrue(
                Revision.objects.for_instance(self.snippet)
                .exclude(approved_go_live_at__isnull=True)
                .exists()
            )

            with self.assertNumQueries(15):
                with self.captureOnCommitCallbacks(execute=True):
                    management.call_command("publish_scheduled")

            self.snippet.refresh_from_db()
            self.assertTrue(self.snippet.live)
            self.assertTrue(self.snippet.first_published_at)
            self.assertFalse(self.snippet.has_unpublished_changes)
            self.assertFalse(
                Revision.objects.for_instance(self.snippet)
                .exclude(approved_go_live_at__isnull=True)
                .exists()
            )

            # Check that the published signal was fired
            self.assertTrue(signal_fired[0])
            self.assertEqual(signal_obj[0], self.snippet)
        finally:
            published.disconnect(published_handler)

    def test_go_live_created_by_editor_will_be_published(self):
        # Connect a mock signal handler to published signal
        signal_fired = [False]
        signal_obj = [None]

        editor = self.create_user("ed")
        editor.groups.add(Group.objects.get(name="Site-wide editors"))

        def published_handler(sender, instance, **kwargs):
            signal_fired[0] = True
            signal_obj[0] = instance

        published.connect(published_handler)

        try:
            go_live_at = timezone.now() - timedelta(days=1)
            self.snippet.has_unpublished_changes = True
            self.snippet.go_live_at = go_live_at

            self.snippet.save_revision(user=editor, approved_go_live_at=go_live_at)

            self.snippet.refresh_from_db()
            self.assertFalse(self.snippet.live)
            self.assertTrue(
                Revision.objects.for_instance(self.snippet)
                .exclude(approved_go_live_at__isnull=True)
                .exists()
            )

            with self.assertNumQueries(15):
                with self.captureOnCommitCallbacks(execute=True):
                    management.call_command("publish_scheduled")

            self.snippet.refresh_from_db()
            self.assertTrue(self.snippet.live)
            self.assertTrue(self.snippet.first_published_at)
            self.assertFalse(self.snippet.has_unpublished_changes)
            self.assertFalse(
                Revision.objects.for_instance(self.snippet)
                .exclude(approved_go_live_at__isnull=True)
                .exists()
            )

            # Check that the published signal was fired
            self.assertTrue(signal_fired[0])
            self.assertEqual(signal_obj[0], self.snippet)
        finally:
            published.disconnect(published_handler)

    def test_go_live_when_newer_revision_exists(self):
        go_live_at = timezone.now() - timedelta(days=1)
        self.snippet.has_unpublished_changes = True
        self.snippet.go_live_at = go_live_at

        self.snippet.save_revision(approved_go_live_at=go_live_at)

        self.snippet.text = "Goodbye world!"
        self.snippet.save_revision()

        with self.assertNumQueries(15):
            with self.captureOnCommitCallbacks(execute=True):
                management.call_command("publish_scheduled")

        self.snippet.refresh_from_db()
        self.assertTrue(self.snippet.live)
        self.assertTrue(self.snippet.has_unpublished_changes)
        self.assertEqual(self.snippet.text, "Hello world!")

    def test_future_go_live_will_not_be_published(self):
        self.snippet.has_unpublished_changes = True
        self.snippet.go_live_at = timezone.now() + timedelta(days=1)

        self.snippet.save_revision(
            approved_go_live_at=timezone.now() - timedelta(days=1)
        )

        self.snippet.refresh_from_db()
        self.assertFalse(self.snippet.live)
        self.assertTrue(
            Revision.objects.for_instance(self.snippet)
            .exclude(approved_go_live_at__isnull=True)
            .exists()
        )

        with self.assertNumQueries(14):
            with self.captureOnCommitCallbacks(execute=True):
                management.call_command("publish_scheduled")

        self.assertFalse(self.snippet.live)
        self.assertTrue(
            Revision.objects.for_instance(self.snippet)
            .exclude(approved_go_live_at__isnull=True)
            .exists()
        )

    def test_expired_will_be_unpublished(self):
        # Connect a mock signal handler to unpublished signal
        signal_fired = [False]
        signal_obj = [None]

        def unpublished_handler(sender, instance, **kwargs):
            signal_fired[0] = True
            signal_obj[0] = instance

        unpublished.connect(unpublished_handler)

        try:
            self.snippet.expire_at = timezone.now() - timedelta(days=1)
            self.snippet.save_revision().publish()

            self.snippet.refresh_from_db()
            self.assertTrue(self.snippet.live)

            with self.assertNumQueries(10):
                with self.captureOnCommitCallbacks(execute=True):
                    management.call_command("publish_scheduled")

            self.snippet.refresh_from_db()
            self.assertFalse(self.snippet.live)
            self.assertTrue(self.snippet.has_unpublished_changes)
            self.assertTrue(self.snippet.expired)

            # Check that the unpublished signal was fired
            self.assertTrue(signal_fired[0])
            self.assertEqual(signal_obj[0], self.snippet)
        finally:
            unpublished.disconnect(unpublished_handler)

    def test_future_expired_will_not_be_unpublished(self):
        self.snippet.expire_at = timezone.now() + timedelta(days=1)
        self.snippet.save_revision().publish()

        self.snippet.refresh_from_db()
        self.assertTrue(self.snippet.live)

        with self.assertNumQueries(6):
            with self.captureOnCommitCallbacks(execute=True):
                management.call_command("publish_scheduled")

        self.snippet.refresh_from_db()
        self.assertTrue(self.snippet.live)
        self.assertFalse(self.snippet.expired)


class TestPurgeRevisionsCommandForPages(TestCase):
    base_options = {}

    def setUp(self):
        self.object = self.get_object()

    def get_object(self):
        # Find root page
        self.root_page = Page.objects.get(id=2)
        self.page = SimplePage(
            title="Hello world!",
            slug="hello-world",
            content="hello",
            live=False,
        )
        self.root_page.add_child(instance=self.page)
        self.page.refresh_from_db()
        return self.page

    def assertRevisionNotExists(self, revision):
        self.assertFalse(Revision.objects.filter(id=revision.id).exists())

    def assertRevisionExists(self, revision):
        self.assertTrue(Revision.objects.filter(id=revision.id).exists())

    def run_command(self, **options):
        return management.call_command(
            "purge_revisions", **{**self.base_options, **options}, stdout=StringIO()
        )

    def test_latest_revision_not_purged(self):
        revision_1 = self.object.save_revision()
        revision_2 = self.object.save_revision()

        self.run_command()

        # revision 1 should be deleted, revision 2 should not be
        self.assertRevisionNotExists(revision_1)
        self.assertRevisionExists(revision_2)

    def test_revisions_in_moderation_or_workflow_not_purged(self):
        workflow = Workflow.objects.create(name="test_workflow")
        task_1 = Task.objects.create(name="test_task_1")
        user = get_user_model().objects.first()
        WorkflowTask.objects.create(workflow=workflow, task=task_1, sort_order=1)

        revision = self.object.save_revision()
        workflow.start(self.object, user)

        # Save a new revision to ensure that the revision in the workflow
        # is not the latest one
        self.object.save_revision()

        self.run_command()

        # even though they're no longer the latest revisions, the old revisions
        # should stay as they are attached to an in progress workflow
        self.assertRevisionExists(revision)

        # If workflow is disabled at some point after that, the revision should
        # be deleted
        with override_settings(WAGTAIL_WORKFLOW_ENABLED=False):
            self.run_command()
            self.assertRevisionNotExists(revision)

    def test_revisions_with_approve_go_live_not_purged(self):
        revision = self.object.save_revision(
            approved_go_live_at=timezone.now() + timedelta(days=1)
        )

        # Save a new revision to ensure that the approved revision
        # is not the latest one
        self.object.save_revision()

        self.run_command()

        self.assertRevisionExists(revision)

    def test_purge_revisions_with_date_cutoff(self):
        old_revision = self.object.save_revision()

        self.object.save_revision()

        self.run_command(days=30)

        # revision should not be deleted, as it is younger than 30 days
        self.assertRevisionExists(old_revision)

        old_revision.created_at = timezone.now() - timedelta(days=31)
        old_revision.save()

        self.run_command(days=30)

        # revision is now older than 30 days, so should be deleted
        self.assertRevisionNotExists(old_revision)

    def test_purge_revisions_protected_error(self):
        revision_old = self.object.save_revision()
        PurgeRevisionsProtectedTestModel.objects.create(revision=revision_old)
        revision_purged = self.object.save_revision()
        self.object.save_revision()

        self.run_command()
        # revision should not be deleted, as it is protected
        self.assertRevisionExists(revision_old)
        # Any other revisions are deleted
        self.assertRevisionNotExists(revision_purged)


class TestPurgeRevisionsCommandForSnippets(TestPurgeRevisionsCommandForPages):
    def get_object(self):
        return FullFeaturedSnippet.objects.create(text="Hello world!")


class TestPurgeRevisionsCommandForPagesWithPagesOnly(TestPurgeRevisionsCommandForPages):
    base_options = {"pages": True}


class TestPurgeRevisionsCommandForPagesWithNonPagesOnly(
    TestPurgeRevisionsCommandForPages
):
    base_options = {"non_pages": True}

    def assertRevisionNotExists(self, revision):
        # Page revisions won't be purged if only non_pages is specified
        return self.assertRevisionExists(revision)


class TestPurgeRevisionsCommandForSnippetsWithNonPagesOnly(
    TestPurgeRevisionsCommandForSnippets
):
    base_options = {"non_pages": True}


class TestPurgeRevisionsCommandForSnippetsWithPagesOnly(
    TestPurgeRevisionsCommandForSnippets
):
    base_options = {"pages": True}

    def assertRevisionNotExists(self, revision):
        # Snippet revisions won't be purged if only pages is specified
        return self.assertRevisionExists(revision)


class TestPurgeEmbedsCommand(TestCase):
    fixtures = ["test.json"]

    def setUp(self):
        # create dummy Embed objects
        for i in range(5):
            embed = Embed(
                hash=f"{i}",
                url="https://www.youtube.com/watch?v=Js8dIRxwSRY",
                max_width=None,
                type="video",
                html="test html",
                title="test title",
                author_name="test author name",
                provider_name="test provider name",
                thumbnail_url="http://test/thumbnail.url",
                width=1000,
                height=1000,
            )
            embed.save()

    def test_purge_embeds(self):
        """
        fetch all dummy embeds and confirm they are deleted when the management command runs

        """

        self.assertEqual(Embed.objects.count(), 5)

        management.call_command("purge_embeds", stdout=StringIO())

        self.assertEqual(Embed.objects.count(), 0)


class TestCreateLogEntriesFromRevisionsCommand(TestCase):
    fixtures = ["test.json"]

    def setUp(self):
        self.page = SimplePage(
            title="Hello world!",
            slug="hello-world",
            content="hello",
            live=False,
            expire_at=timezone.now() - timedelta(days=1),
        )

        Page.objects.get(id=2).add_child(instance=self.page)

        # Create empty revisions, which should not be converted to log entries
        for i in range(3):
            self.page.save_revision()

        # Add another revision with a content change
        self.page.title = "Hello world!!"
        revision = self.page.save_revision()
        revision.publish()

        # Do the same with a SecretPage (to check that the version comparison code doesn't
        # trip up on permission-dependent edit handlers)
        self.secret_page = SecretPage(
            title="The moon",
            slug="the-moon",
            boring_data="the moon",
            secret_data="is made of cheese",
            live=False,
        )

        Page.objects.get(id=2).add_child(instance=self.secret_page)

        # Create empty revisions, which should not be converted to log entries
        for i in range(3):
            self.secret_page.save_revision()

        # Add another revision with a content change
        self.secret_page.secret_data = "is flat"
        revision = self.secret_page.save_revision()
        revision.publish()

        # clean up log entries
        PageLogEntry.objects.all().delete()

    def test_log_entries_created_from_revisions(self):
        management.call_command("create_log_entries_from_revisions")

        # Should not create entries for empty revisions.
        self.assertListEqual(
            list(PageLogEntry.objects.values_list("page_id", "action")),
            # Default PageLogEntry sort order is from newest event to oldest.
            # We reverse here to make it easier to understand what is being
            # tested. The events here should correspond with setUp above.
            list(
                reversed(
                    [
                        # The SimplePage was created in draft mode, with an initial revision.
                        (self.page.pk, "wagtail.create"),
                        (self.page.pk, "wagtail.edit"),
                        # The SimplePage was edited as a new draft, then published.
                        (self.page.pk, "wagtail.edit"),
                        (self.page.pk, "wagtail.publish"),
                        # The SecretPage was created in draft mode, with an initial revision.
                        (self.secret_page.pk, "wagtail.create"),
                        (self.secret_page.pk, "wagtail.edit"),
                        # The SecretPage was edited as a new draft, then published.
                        (self.secret_page.pk, "wagtail.edit"),
                        (self.secret_page.pk, "wagtail.publish"),
                    ]
                )
            ),
        )

    def test_command_doesnt_crash_for_revisions_without_page_model(self):
        with mock.patch(
            "wagtail.models.Page.specific_class",
            return_value=None,
            new_callable=mock.PropertyMock,
        ):
            management.call_command("create_log_entries_from_revisions")
            self.assertEqual(PageLogEntry.objects.count(), 0)
