# Generated by Django 4.2.1 on 2023-06-14 15:43

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("wagtailcore", "0083_workflowcontenttype"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="page",
            options={
                "permissions": [
                    ("bulk_delete_page", "Delete pages with children"),
                    ("lock_page", "Lock/unlock pages you've locked"),
                    ("publish_page", "Publish any page"),
                    ("unlock_page", "Unlock any page"),
                ],
                "verbose_name": "page",
                "verbose_name_plural": "pages",
            },
        ),
    ]