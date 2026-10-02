"""Offline exact-source and declared-contract binding for held-out packets."""
import hashlib
import re


def validate_packet_context(packet):
    """Verify exact declared fresh-corpus fields are in the submitted prompt."""
    source, contract = packet.get('source'), packet.get('contract')
    revision, path, identifier = packet.get('revision'), packet.get('path'), packet.get('case_id')
    messages = packet.get('messages')
    if (not isinstance(source, str) or not isinstance(contract, str) or not contract
            or not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}', revision)
            or revision != hashlib.sha1(('synthetic\0' + source + contract).encode()).hexdigest()
            or not isinstance(identifier, str) or path != identifier + '.py'
            or not isinstance(messages, list) or len(messages) != 2
            or not all(isinstance(message, dict) for message in messages)
            or [message.get('role') for message in messages] != ['system', 'user']
            or any(not isinstance(message.get('content'), str) for message in messages)):
        raise ValueError('invalid_context_binding')
    expected_user = (f'head_sha: {revision}\npath: {path}\nReviewed contract:\n'
                     f'{contract}\nComplete source:\n{source}')
    if messages[1]['content'] != expected_user:
        raise ValueError('context_assembly_mismatch')
