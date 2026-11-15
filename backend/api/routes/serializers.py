from ..pipeline.states import phase


def short_data(short):
    base = f'/api/v1/shorts/{short.pk}'
    return dict(id=str(short.id), short_number=short.short_number, start_time=short.start_time,
        end_time=short.end_time, duration=short.duration, title=short.title, description=short.description,
        hashtags=short.hashtags, transcript=short.transcript, quality_score=short.quality_score,
        quality_report=short.quality_report, rights_review_status=short.rights_review_status,
        segments=[dict(id=str(seg.id),start_ms=seg.start_ms,end_ms=seg.end_ms,original_text=seg.original_text,
            translated_english=seg.translated_english,translated_hindi=seg.translated_hindi,
            detected_language=seg.detected_language,confidence=seg.confidence) for seg in short.segments.all()],
        video_url=base+'/video', download_url=base+'/download', thumbnail_url=base+'/thumbnail',
        caption_url=base+'/captions' if short.caption_file else None,
        project_id=str(short.project_id) if short.project_id else None, kind=short.kind, qa_status=short.qa_status,
        timeline_summary=(short.quality_report or {}).get('timeline_summary'))


def job_data(job):
    source = job.source
    return dict(id=str(job.id), status=job.status, stage=job.stage, progress=job.progress,
        error_code=job.error_code, error_message=job.error_message, settings=job.settings,
        analysis={k:v for k,v in job.analysis.items() if k not in {'signals','candidates'}}, created_at=job.created_at.isoformat(),
        title=(source.title if source else '') or (job.project.title if job.project_id else '') or 'New generation',
        source_url=source.source_url if source else '', permission_reference=source.permission_reference if source else '',
        rights_declaration=source.rights_declaration if source else False,
        license_status=source.license_status if source else '',
        results=[short_data(s) for s in job.shorts.all()],
        project_id=str(job.project_id) if job.project_id else None, pipeline=job.pipeline,
        phase=phase(job.status), kind=job.pipeline)
