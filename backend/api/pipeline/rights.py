"""Rights-review block attached to every generated output's quality report."""


def build_rights_review(source, metadata, info, commentary):
    return {
        'source_url': source.source_url, 'original_creator': metadata.get('uploader') or metadata.get('channel') or 'Not independently verified',
        'rights_declaration': source.rights_declaration, 'permission_reference': source.permission_reference,
        'permission_verification': 'User declaration only; not independently verified',
        'attribution_requirements': 'Check the source license or permission terms',
        'music_rights': 'Unknown; original source audio retained' if info['has_audio'] else 'No source audio',
        'third_party_footage': 'Not automatically determined',
        'originality': 'Grounded on-screen analysis based on quoted source speech' if commentary else 'Source-based highlight with captions and reframing; no original commentary',
        'copyright_permission': 'Requires review', 'reused_content_eligibility': 'Requires independent YouTube policy review'}
