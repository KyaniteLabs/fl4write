"""Offline captured-output scorer; trusted adjudications are a caller boundary.

Never sends requests, runs source, writes captures, or derives judgments from
seed labels. Receipts bind supplied evidence, not reviewer independence/truth.
Only captured cases enter checkpoint recall. Full-corpus coverage is separate.
"""
import argparse
import hashlib
import json
from pathlib import Path

from claim_gate import SEVERITIES, assess, bind_claim, detailed_confirmation, digest, source_index
from evaluation_gate import known, sha256, validity
from packet_context import validate_packet_context
from product_acceptance import evaluate
from review_transport import strict_envelope, strict_json


def text_sha256(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def snapshot_for(packet):
    source = packet['source']
    lines = source.split('\n')
    if lines[-1] == '':
        lines.pop()
    expected = {packet['path']: dict(enumerate(lines, start=1))}
    if 'snapshot' in packet:
        snapshot = packet['snapshot']
        if (not isinstance(snapshot, dict) or snapshot.get('head_sha') != packet['revision']
                or source_index(snapshot) != expected):
            raise ValueError('snapshot_packet_mismatch')
        return snapshot
    return {'repository': 'synthetic/fl4write-held-out', 'head_sha': packet['revision'],
            'files': [{'path': packet['path'],
                       'patch': '@@ -0,0 +1,' + str(len(lines)) + ' @@\n'
                                + ''.join('+' + line + '\n' for line in lines)}]}


def _gate(envelope, packet, evidence, judgments, normalize_indent):
    """Source binding uses actual capture completeness, not labels or defaults."""
    try:
        snapshot = snapshot_for(packet)
        if not isinstance(snapshot, dict):
            raise ValueError('invalid_source_snapshot')
    except (ValueError, TypeError, KeyError, RecursionError):
        return {'status': 'abstained', 'actionable': 0, 'actionable_findings': [],
                'grounded': 0, 'invalid': 0,
                'blockers': ['invalid_source_snapshot']}
    run = {'repository': snapshot.get('repository'), 'head_sha': snapshot.get('head_sha'),
           'snapshot_sha256': digest(snapshot),
           'input_complete': evidence.get('input_truncated') is False,
           'response_complete': evidence.get('finish_reason') == 'stop'
                                and evidence.get('output_truncated') is False,
           'tools_ok': evidence.get('tools_ok') is True}
    receipts = {}
    try:
        sources = source_index(snapshot)
        for index, judgment in judgments.items():
            claim = envelope['findings'][index]
            try:
                claim_id = bind_claim(claim, snapshot, sources, normalize_indent=normalize_indent)
            except (ValueError, TypeError, RecursionError):
                continue
            receipts[claim_id] = {
                'claim_id': claim_id, 'head_sha': snapshot['head_sha'],
                'snapshot_sha256': digest(snapshot), 'scope': judgment['scope'],
                'reviewer': judgment['reviewer'], 'disposition': judgment['disposition'],
                'verified_severity': judgment.get('verified_severity', claim.get('severity')),
                'basis': {'kind': judgment.get('basis_kind'),
                          'artifact_sha256': judgment['artifact_sha256'],
                          'rationale': judgment.get('rationale')}}
            if 'allegation_checks' in judgment:
                receipts[claim_id]['allegation_checks'] = judgment['allegation_checks']
        result = assess(envelope, snapshot, run, receipts, normalize_indent=normalize_indent)
        actionable_findings = [
            {'claim_index': index, 'claim_id': row['claim_id'],
             'defect_id': judgments[index]['defect_id']}
            for index, row in enumerate(result['claims'])
            if row.get('actionable') is True and index in judgments
            and judgments[index]['disposition'] == 'confirmed']
        confirmation_blockers = [
            'independent_source_confirmation_missing:' + str(index)
            for index, row in enumerate(result['claims'])
            if row.get('status') == 'grounded_unvalidated' and index in judgments
            and judgments[index]['disposition'] == 'confirmed']
        return {'status': result['status'], 'actionable': len(result['actionable']),
                'actionable_findings': actionable_findings,
                'grounded': sum(row.get('status') not in ('invalid', 'duplicate')
                                for row in result['claims']),
                'invalid': sum(row.get('status') == 'invalid' for row in result['claims']),
                'blockers': result['blockers'] + confirmation_blockers}
    except (ValueError, TypeError, KeyError, RecursionError):
        return {'status': 'abstained', 'actionable': 0, 'actionable_findings': [],
                'grounded': 0, 'invalid': 0,
                'blockers': ['invalid_source_snapshot']}


def evaluate_captures(manifest, packets, captures, judgments, selected_case_ids=None,
                      normalize_presentation=False):
    """Capture/judgment maps are keyed by case_id; no input is mutated.

    Capture: visible_content, visible_content_sha256, input_sha256,
    run_evidence, safety_evidence. Judgment: exact claim_index/claim_sha256,
    capture_content_sha256/input_sha256, scope whole_allegation, disposition,
    reviewer/artifact_sha256; confirmed defect_id must belong to this case.
    Optional basis_kind/rationale/verified_severity enable the separate source
    gate. Runtime controls and safety observations must come from their owner.
    """
    output = {'schema_version': 1, 'new_model_calls': 0, 'deployment_accepted': False,
              'merge_recommendation': 'withheld', 'blockers': [], 'capture_results': {},
              'trust_boundary': 'caller-supplied independent adjudications; digests do not prove truth',
              'profile': 'normalized_presentation' if normalize_presentation else 'strict_json'}
    blockers = output['blockers']
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1
            or not isinstance(manifest.get('cases'), list)
            or not all(isinstance(item, dict) for item in manifest['cases'])
            or not all(isinstance(item, dict) for item in (packets, captures, judgments))):
        blockers.append('invalid_inputs')
        output['status'] = 'inconclusive'
        return output
    rows = manifest['cases']
    ids = [case.get('case_id') for case in rows]
    if any(not known(identifier) for identifier in ids) or len(set(ids)) != len(ids):
        blockers.append('invalid_or_duplicate_manifest_case')
        output['status'] = 'inconclusive'
        return output
    if any(not isinstance(case.get('defect_ids'), list)
           or any(not known(defect) for defect in case['defect_ids']) for case in rows):
        blockers.append('invalid_defect_labels')
        output['status'] = 'inconclusive'
        return output
    if selected_case_ids is not None and (not isinstance(selected_case_ids, (list, tuple, set))
            or any(not known(identifier) for identifier in selected_case_ids)):
        blockers.append('invalid_selected_cases')
        output['status'] = 'inconclusive'
        return output
    if 'case_set_sha256' in manifest and manifest['case_set_sha256'] != digest(rows):
        blockers.append('manifest_case_set_hash_mismatch')
    if 'packets_sha256' in manifest:
        try:
            hashes = {identifier: digest(packets[identifier]) for identifier in ids}
            # The frozen V2 held-out subset used packet_sha; the full corpus
            # used packet_sha256. Both exact serialization schemes retain
            # byte-equivalent packet binding without rewriting custody.
            accepted_hashes = {digest([{'case_id': identifier, key: hashes[identifier]}
                                      for identifier in ids])
                               for key in ('packet_sha256', 'packet_sha')}
            if manifest['packets_sha256'] not in accepted_hashes:
                blockers.append('packet_manifest_hash_mismatch')
        except (KeyError, TypeError, ValueError, RecursionError):
            blockers.append('packet_manifest_unverified')
    requested = set(ids) if selected_case_ids is None else set(selected_case_ids)
    if not requested.issubset(ids):
        blockers.append('unknown_selected_case')
    unknown_captures = set(captures) - set(ids)
    if unknown_captures:
        blockers.append('unknown_capture_case')
    observed = requested.intersection(ids).intersection(captures)
    selected = [case for case in rows if case['case_id'] in observed]
    observations = []
    unjudged_claims = 0
    for case in selected:
        identifier = case['case_id']
        errors, accepted = [], {}
        capture, packet = captures[identifier], packets.get(identifier)
        result = {'status': 'abstained', 'errors': errors, 'claim_count': None,
                  'unjudged_claims': 0, 'effective_budget_observable': False}
        output['capture_results'][identifier] = result
        evidence = capture.get('run_evidence') if isinstance(capture, dict) else None
        evidence = evidence if isinstance(evidence, dict) else {}
        evidence_check = validity(evidence)
        effective = evidence.get('effective_controls')
        result['run_validity'] = evidence_check
        result['effective_budget_observable'] = (
            isinstance(effective, dict)
            and effective.get('evidence_origin') in ('backend_response', 'backend_task_log')
            and type(effective.get('reasoning_budget_tokens')) is int
            and effective['reasoning_budget_tokens'] >= 0)
        observation = {'case_id': identifier, 'outcome': 'abstained', 'claims': [],
                       'run_evidence': evidence,
                       'safety_evidence': capture.get('safety_evidence') if isinstance(capture, dict) else None}
        observations.append(observation)
        if not isinstance(capture, dict) or not isinstance(packet, dict):
            errors.append('capture_or_packet_missing')
            continue
        content = capture.get('visible_content')
        if (not isinstance(content, str) or not sha256(capture.get('visible_content_sha256'))
                or text_sha256(content) != capture.get('visible_content_sha256')):
            errors.append('capture_content_hash_mismatch')
            continue
        result['capture_content_sha256'] = capture['visible_content_sha256']
        try:
            if 'contract' in packet or 'family' in case:
                validate_packet_context(packet)
            if (packet.get('case_id') != identifier
                    or digest(packet.get('messages')) != case.get('input_sha256')
                    or capture.get('input_sha256') != case.get('input_sha256')
                    or text_sha256(packet['source']) != case.get('source_sha256')):
                errors.append('packet_or_capture_input_mismatch')
                continue
            envelope = strict_envelope(content, normalize_presentation=normalize_presentation)
        except (ValueError, TypeError, KeyError, RecursionError):
            errors.append('invalid_packet_or_strict_json')
            continue
        findings = envelope['findings']
        result['claim_count'] = len(findings)
        supplied = judgments.get(identifier, [])
        if not isinstance(supplied, list):
            errors.append('invalid_judgment_collection')
            supplied = []
        candidates = {}
        for judgment in supplied:
            if not isinstance(judgment, dict) or type(judgment.get('claim_index')) is not int:
                errors.append('invalid_independent_judgment')
                continue
            index = judgment['claim_index']
            if not 0 <= index < len(findings):
                errors.append('unknown_judgment_index')
                continue
            candidates.setdefault(index, []).append(judgment)
        for index, claim in enumerate(findings):
            candidates_at_index = candidates.get(index, [])
            if not isinstance(claim, dict) or claim.get('severity') not in SEVERITIES:
                errors.append('invalid_claim_shape_or_severity')
                continue
            if len(candidates_at_index) != 1:
                errors.append('missing_or_duplicate_judgment')
                continue
            judgment = candidates_at_index[0]
            if (judgment.get('claim_sha256') != digest(claim)
                    or judgment.get('capture_content_sha256') != capture['visible_content_sha256']
                    or judgment.get('input_sha256') != case['input_sha256']
                    or judgment.get('scope') != 'whole_allegation'
                    or judgment.get('disposition') not in ('confirmed', 'false_positive', 'unsupported')
                    or not known(judgment.get('reviewer'))
                    or not sha256(judgment.get('artifact_sha256'))):
                errors.append('unbound_independent_judgment')
                continue
            if (judgment['disposition'] == 'confirmed'
                    and judgment.get('defect_id') not in case.get('defect_ids', [])):
                errors.append('confirmed_defect_not_in_same_case')
                continue
            if judgment['disposition'] == 'confirmed' and not detailed_confirmation(claim, judgment):
                errors.append('invalid_or_contradictory_allegation_checks')
                continue
            accepted[index] = judgment
            observation['claims'].append({
                'claim_sha256': digest(claim), 'scope': 'whole_allegation',
                'disposition': judgment['disposition'], 'defect_id': judgment.get('defect_id'),
                'reviewer': judgment['reviewer'], 'artifact_sha256': judgment['artifact_sha256'],
                # Reported severity matters for severe false positives.
                'severity': claim['severity']})
        duplicate_hashes = {}
        for scored_claim in observation['claims']:
            key = scored_claim['claim_sha256']
            fingerprint = digest(scored_claim)
            if key in duplicate_hashes and duplicate_hashes[key] != fingerprint:
                errors.append('conflicting_duplicate_claim_judgment')
            duplicate_hashes[key] = fingerprint
        result['unjudged_claims'] = len(findings) - len(accepted)
        unjudged_claims += result['unjudged_claims']
        if not errors:
            observation['outcome'] = 'completed'
            result['status'] = 'adjudicated'
        else:
            # Partial receipts cannot turn the entire output into an accepted run.
            observation['claims'] = [claim for claim in observation['claims']
                                     if claim['disposition'] != 'confirmed']
        gate_judgments = accepted if not errors else {}
        result['strict_gate'] = _gate(envelope, packet, evidence, gate_judgments, False)
        if normalize_presentation:
            result['normalized_gate'] = _gate(envelope, packet, evidence, gate_judgments, True)
        try:
            strict_envelope(content)
        except (ValueError, TypeError, RecursionError):
            result['strict_gate'] = {'status': 'abstained', 'actionable': 0,
                                     'actionable_findings': [],
                                     'grounded': 0, 'invalid': 0,
                                     'blockers': ['strict_json_presentation_invalid']}
    for identifier, result in output['capture_results'].items():
        if result['errors']:
            blockers.append('capture_or_judgment_invalid:' + identifier)
    checkpoint = evaluate(selected, observations)
    output['checkpoint'] = checkpoint
    output['correlation_warning'] = manifest.get('correlation_warning')
    output['observed_case_set_sha256'] = digest(selected)
    output['observed_case_ids'] = [case['case_id'] for case in selected]
    output['unexecuted_case_ids'] = [identifier for identifier in ids if identifier not in observed]
    output['unjudged_claims'] = unjudged_claims
    expected = {defect for case in selected for defect in case.get('defect_ids', [])}
    source_profiles = {}
    for profile, key in [('strict_json', 'strict_gate')] + (
            [('normalized_presentation', 'normalized_gate')] if normalize_presentation else []):
        detected, source_blockers = set(), []
        actionable_count = 0
        for case in selected:
            identifier = case['case_id']
            gated = output['capture_results'][identifier].get(key)
            if gated is None:
                source_blockers.append('source_evaluation_missing:' + identifier)
                continue
            if gated['blockers']:
                source_blockers.append('source_evidence_incomplete:' + identifier)
            # Only exact confirmed actionables returned by assess qualify. A
            # grounded quote, claim count or seeded case label never detects it.
            for finding in gated.get('actionable_findings', []):
                if finding['defect_id'] in case.get('defect_ids', []):
                    detected.add(finding['defect_id'])
                    actionable_count += 1
        recall = len(expected & detected) / len(expected) if expected else None
        failures = ['source_actionable_recall_below_target'] if recall is None or recall < .90 else []
        source_profiles[profile] = {
            'metric_kind': 'adjudication-mediated usability; not model precision',
            'expected_seeded_defects': len(expected),
            'confirmed_actionable_defects': len(expected & detected),
            'missed_seeded_defects': len(expected - detected), 'recall': recall,
            'actionable_findings': actionable_count,
            'adjudicated_actionable_false_claims': 0,
            'false_claim_note': 'False/unsupported claims are withheld by caller judgments; this is not a model accuracy estimate.',
            'recall_target': .90, 'blockers': sorted(set(source_blockers)),
            'quality_failures': failures,
            'status': 'inconclusive' if source_blockers else (
                'failed_usability_target' if failures else 'passed_usability_target')}
    output['source_actionable'] = source_profiles
    selected_source = source_profiles[output['profile']]
    source_blockers = selected_source['blockers']
    engineering_failures = sorted(set(checkpoint['quality_failures'] + selected_source['quality_failures']))
    engineering_status = checkpoint['status']
    if blockers or source_blockers:
        engineering_status = 'inconclusive'
    elif engineering_status != 'inconclusive' and engineering_failures:
        engineering_status = 'failed_engineering_target'
    output['engineering_quality_failures'] = engineering_failures
    output['full_corpus_gate'] = {
        'status': 'inconclusive' if len(observed) != len(ids) else engineering_status,
        'source_profile': output['profile'], 'source_actionable_recall_target': .90,
        'quality_failures': engineering_failures,
        'manifest_cases': len(ids), 'observed_cases': len(observed),
        'unexecuted_cases_are_model_misses': False,
        'blockers': sorted(set(blockers + checkpoint['blockers'] + source_blockers
                               + (['incomplete_corpus_coverage'] if len(observed) != len(ids) else [])))}
    output['status'] = output['full_corpus_gate']['status']
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('captures', type=Path)
    parser.add_argument('judgments', type=Path)
    parser.add_argument('--case-id', action='append', dest='case_ids')
    parser.add_argument('--normalize-presentation', action='store_true')
    args = parser.parse_args()
    manifest = strict_json(args.manifest.read_text())
    packets = {}
    for case in manifest['cases']:
        path = args.manifest.parent / case['packet_file']
        if not path.resolve().is_relative_to(args.manifest.parent.resolve()):
            raise ValueError('packet_path_outside_corpus')
        packets[case['case_id']] = strict_json(path.read_text())
    result = evaluate_captures(manifest, packets, strict_json(args.captures.read_text()),
                               strict_json(args.judgments.read_text()), args.case_ids,
                               args.normalize_presentation)
    # Findings, source, prompts, reasoning and raw captures are never printed.
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
