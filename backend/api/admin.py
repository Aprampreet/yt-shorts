from django.contrib import admin

from .models import (GeneratedScript, GeneratedShort, GenerationJob, GenerationProject, MediaAsset,
                     ProviderUsage, ScheduledPost, StoryboardScene, StoryConcept)


@admin.register(GenerationProject)
class GenerationProjectAdmin(admin.ModelAdmin):
    list_display = ('id', 'kind', 'title', 'owner', 'status', 'created_at')
    list_filter = ('kind', 'status')
    search_fields = ('title', 'owner')


@admin.register(GenerationJob)
class GenerationJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'pipeline', 'step', 'status', 'progress', 'owner', 'created_at')
    list_filter = ('pipeline', 'status')
    search_fields = ('owner',)
    raw_id_fields = ('source', 'project')


@admin.register(GeneratedShort)
class GeneratedShortAdmin(admin.ModelAdmin):
    list_display = ('id', 'kind', 'short_number', 'title', 'qa_status', 'rights_review_status', 'created_at')
    list_filter = ('kind', 'qa_status', 'rights_review_status')
    raw_id_fields = ('job', 'project', 'script')


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ('id', 'kind', 'title', 'owner', 'license_status', 'origin', 'pinned', 'deleted_at', 'created_at')
    list_filter = ('kind', 'license_status', 'origin', 'pinned')
    search_fields = ('title', 'owner', 'sha256')
    raw_id_fields = ('source_video',)


@admin.register(StoryConcept)
class StoryConceptAdmin(admin.ModelAdmin):
    list_display = ('id', 'project', 'index', 'angle', 'title', 'selected')
    list_filter = ('angle', 'selected')
    raw_id_fields = ('project',)


@admin.register(GeneratedScript)
class GeneratedScriptAdmin(admin.ModelAdmin):
    list_display = ('id', 'project', 'concept', 'version', 'language', 'status', 'created_at')
    list_filter = ('status', 'language')
    raw_id_fields = ('project', 'concept', 'narration_asset')


@admin.register(StoryboardScene)
class StoryboardSceneAdmin(admin.ModelAdmin):
    list_display = ('id', 'script', 'order', 'start', 'end', 'visual_type', 'rights_status')
    list_filter = ('visual_type',)
    raw_id_fields = ('script', 'asset')


@admin.register(ProviderUsage)
class ProviderUsageAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'provider', 'capability', 'billing', 'status', 'units', 'unit_type', 'cost_usd_estimate')
    list_filter = ('provider', 'billing', 'status', 'capability')
    raw_id_fields = ('job',)


@admin.register(ScheduledPost)
class ScheduledPostAdmin(admin.ModelAdmin):
    list_display = ('id', 'platform', 'status', 'scheduled_at', 'owner')
    list_filter = ('platform', 'status')
    raw_id_fields = ('generated_short', 'social_account')
