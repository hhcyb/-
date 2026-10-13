"""Standalone online runner for the frozen PrimeVul prompts (Python 3.8+, standard library only).

run:   send prepared/<condition>.jsonl to an OpenAI-compatible endpoint (DashScope by default), concurrently,
       resumable; every prompt is byte-identical to the local experiments.
score: pair metrics (P-C / P-V / P-B / P-R) for every finished run, McNemar against a baseline run,
       on all 435 pairs and on the dev-A subset. Labels are read only here.
"""
from __future__ import annotations

import argparse
from collections import Counter
import concurrent.futures as cf
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
PREPARED = HERE.parent / 'prepared'
VERDICTS = {1: 1, 0: 0, True: 1, False: 0, '1': 1, '0': 0, 'true': 1, 'false': 0, 'yes': 1, 'no': 0}
RETRY_PROMPT = 'Output only the final JSON object now.'


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def read_rows(path):
    with open(path, encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def extract_json(raw):
    """Last top-level JSON object in a reply; tolerates <think> blocks and code fences (same as the local runs)."""
    text = re.sub(r'<think>.*?</think>', '', str(raw or ''), flags=re.S)
    depth, start, found = 0, None, None
    for i, c in enumerate(text):
        if c == '{':
            depth, start = depth + 1, i if depth == 0 else start
        elif c == '}' and depth:
            depth -= 1
            if depth == 0:
                found = text[start:i + 1]
    if found is None:
        raise ValueError('No JSON object in reply')
    return json.loads(found)


def parse_verdict(raw):
    value = extract_json(raw)
    verdict = value.get('vulnerable')
    key = verdict.strip().lower() if isinstance(verdict, str) else verdict
    if key not in VERDICTS:
        raise ValueError(f'vulnerable must be 0 or 1, got {verdict!r}')
    return VERDICTS[key]


class PermanentError(Exception):
    pass


def request_body(args, messages, max_tokens, thinking):
    body = {'model': args.model, 'messages': messages, 'max_tokens': max_tokens}
    if args.temperature != 'none':
        body['temperature'] = float(args.temperature)
    if args.seed >= 0:
        body['seed'] = args.seed
    if thinking in ('on', 'off'):
        body['enable_thinking'] = thinking == 'on'
    stream = args.stream == 'on' or (args.stream == 'auto' and thinking == 'on')
    if stream:
        body['stream'] = True
        body['stream_options'] = {'include_usage': True}
    body.update(json.loads(args.extra_body) if args.extra_body else {})
    return body, stream


def post(args, body, stream):
    """One HTTP call. Returns (content, reasoning, finish_reason, usage)."""
    data = json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(args.api_base.rstrip('/') + '/chat/completions', data=data, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + os.environ.get(args.api_key_env, '')})
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        if not stream:
            payload = json.loads(resp.read().decode('utf-8'))
            choice = payload['choices'][0]
            msg = choice.get('message') or {}
            return (msg.get('content') or '', msg.get('reasoning_content') or '', choice.get('finish_reason'),
                    payload.get('usage') or {})
        content, reasoning, finish, usage = [], [], None, {}
        for line in resp:
            line = line.decode('utf-8').strip()
            if not line.startswith('data:'):
                continue
            chunk = line[5:].strip()
            if chunk == '[DONE]':
                break
            event = json.loads(chunk)
            if event.get('usage'):
                usage = event['usage']
            for choice in event.get('choices') or []:
                delta = choice.get('delta') or {}
                content.append(delta.get('content') or '')
                reasoning.append(delta.get('reasoning_content') or '')
                finish = choice.get('finish_reason') or finish
        return ''.join(content), ''.join(reasoning), finish, usage


def call(args, messages, max_tokens, thinking):
    body, stream = request_body(args, messages, max_tokens, thinking)
    for attempt in range(args.retries):
        try:
            return post(args, body, stream)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')[:300]
            if exc.code in (400, 401, 403, 404):
                raise PermanentError(f'HTTP {exc.code}: {detail}')
            error = f'HTTP {exc.code}: {detail}'
        except Exception as exc:  # network, timeout, malformed stream
            error = f'{type(exc).__name__}: {exc}'[:300]
        if attempt == args.retries - 1:
            raise RuntimeError(error)
        time.sleep(min(60, 3 * 2 ** attempt))


def classify(args, row):
    """Same protocol as the local runs: reasoning first, one follow-up for the verdict only if it is missing."""
    status, prediction, error, usage, retry_usage = 'ok', None, None, {}, None
    content = reasoning = finish = ''
    try:
        content, reasoning, finish, usage = call(args, row['messages'], args.max_tokens, args.thinking)
        try:
            prediction = parse_verdict(content)
        except ValueError:
            follow = row['messages'] + [{'role': 'assistant', 'content': content},
                                        {'role': 'user', 'content': RETRY_PROMPT}]
            retry, _, _, retry_usage = call(args, follow, 256, 'off' if args.thinking == 'on' else args.thinking)
            prediction = parse_verdict(retry)
            content, status = content + '\n[verdict-retry]\n' + retry, 'ok_after_retry'
    except PermanentError as exc:
        status, error = 'error:permanent', str(exc)
    except Exception as exc:
        status, error = f'error:{type(exc).__name__}', str(exc)[:300]
    return {'sample_id': row['sample_id'], 'prediction': prediction, 'status': status, 'error': error,
            'finish_reason': finish, 'output_truncated': finish == 'length', 'usage': usage,
            'retry_usage': retry_usage, 'reasoning_chars': len(reasoning or ''), 'raw': (content or '')[-3000:]}


VULTRIAL_PROMPTS = HERE / 'vultrial_prompts.json'


def target_source(row):
    """The target function, recovered exactly from the frozen direct prompt ('TARGET FUNCTION:\\n```c\\n' + src + '\\n```')."""
    content = row['messages'][-1]['content']
    return content.split('TARGET FUNCTION:\n```c\n', 1)[1].rsplit('\n```', 1)[0]


def vultrial_verdict(text):
    """VulTrial's own rule (Evaluation/Script/eval_results_VulTrial.py, status 'action_and_valid_high'), kept as written:
    vulnerable iff some review-board verdict is decision 'valid', severity 'high' and action 'fix immediately'."""
    def check(k):
        if k['recommended_action'] == 'fix immediately' and k['decision'] == 'valid' and k['severity'] == 'high':
            return 1
        return None
    lines = text
    if '</think>' in lines:
        lines = lines.split('</think>')[-1]
        lines = lines.split('```json\n')[-1]
        lines = '```json\n' + lines
    lines_updated = lines.replace('```json\n', '').replace('\n```', '')
    vul_predict = 0
    try:
        vul_predict = 0
        jury = json.loads(lines_updated)
        try:
            for k in jury:
                if check(k):
                    vul_predict = check(k)
        except Exception:
            if 'verdicts' in jury:
                vul_predict = 0
                for k in jury['verdicts']:
                    if check(k):
                        vul_predict = check(k)
    except Exception:
        if 'mitigate' in lines_updated.split('\n')[-1]:
            vul_predict = 1
    return vul_predict


def vultrial(args, row, spec):
    """One VulTrial trial (vultrial_base, max_turns 4): security researcher -> code author -> moderator -> review board.
    Each call is a single message holding the filled template; chat history follows VulTrial's JudgeMemory: the
    researcher's reply resets it, later replies are appended after a blank line, empty replies are skipped."""
    from string import Template
    code = target_source(row)
    transcript, said, usage, reasoning = {}, [], {}, 0
    status, error, prediction = 'ok', None, None
    try:
        for role in spec['order']:
            history = ''
            for sender, content in said:
                history = content if sender == 'security_researcher' else history + '\n\n' + content
            prompt = Template(spec['prompt_template']).safe_substitute(
                role_description=spec['role_description'][role] + spec['code_separator'] + code, chat_history=history)
            content, thought, finish, used = call(args, [{'role': args.vultrial_message_role, 'content': prompt}],
                                                  args.max_tokens, args.thinking)
            transcript[role] = content
            usage[role] = dict(used or {}, finish_reason=finish)
            reasoning += len(thought or '')
            if content:
                said.append((role, content))
        prediction = vultrial_verdict(transcript.get('review_board', ''))
    except PermanentError as exc:
        status, error = 'error:permanent', str(exc)
    except Exception as exc:
        status, error = f'error:{type(exc).__name__}', str(exc)[:300]
    return {'sample_id': row['sample_id'], 'prediction': prediction, 'status': status, 'error': error,
            'output_truncated': any(u.get('finish_reason') == 'length' for u in usage.values()), 'usage': usage,
            'reasoning_chars': reasoning, 'transcript': transcript}


def say(text):
    """Progress output must never stop a run (e.g. a closed console or pipe)."""
    try:
        print(text, flush=True)
    except OSError:
        pass


def run_name(args):
    model = re.sub(r'[^A-Za-z0-9_.-]+', '-', args.model)
    return f'{args.condition}_{model}' + (f'_{args.tag}' if args.tag else '')


def run(args):
    # VulTrial runs on the same frozen target functions as direct; its own prompts come from vultrial_prompts.json.
    prepared = PREPARED / ('direct.jsonl' if args.condition == 'vultrial' else f'{args.condition}.jsonl')
    rows = read_rows(prepared)
    spec = json.loads(VULTRIAL_PROMPTS.read_text(encoding='utf-8')) if args.condition == 'vultrial' else None
    if args.subset == 'dev_a':
        # Only the pair membership flag is read here; labels stay unused until `score`.
        dev = {r['sample_id'] for r in read_rows(PREPARED / 'labels.jsonl') if r.get('dev_a')}
        rows = [r for r in rows if r['sample_id'] in dev]
    if args.limit:
        rows = rows[:args.limit]
    if not os.environ.get(args.api_key_env):
        sys.exit(f'Environment variable {args.api_key_env} is empty; set your API key first.')
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = run_name(args)
    out, cfg_path = out_dir / f'predictions_{name}.jsonl', out_dir / f'run_config_{name}.json'
    config = {k: getattr(args, k) for k in ('condition', 'model', 'api_base', 'max_tokens', 'temperature', 'seed',
                                            'thinking', 'stream', 'extra_body', 'limit')}
    config['prepared_sha256'] = file_sha256(prepared)
    if spec is not None:
        config.update(vultrial_prompts_sha256=file_sha256(VULTRIAL_PROMPTS),
                      vultrial_message_role=args.vultrial_message_role)
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    if cfg_path.exists():
        old = json.loads(cfg_path.read_text(encoding='utf-8'))
        if old.get('config_hash') != config_hash:
            changed = sorted(k for k in set(old['config']) | set(config) if old['config'].get(k) != config.get(k))
            sys.exit(f'{out.name} was made with another configuration (changed: {changed}); use a new --tag.')
    elif out.exists() and out.stat().st_size:
        sys.exit(f'{out.name} exists without {cfg_path.name}; use a new --tag.')
    cfg_path.write_text(json.dumps({'config_hash': config_hash, 'config': config}, indent=1), encoding='utf-8')
    done = {r['sample_id'] for r in read_rows(out) if not str(r.get('status', '')).startswith('error')} \
        if out.exists() else set()
    todo = [r for r in rows if r['sample_id'] not in done]
    say(f'{name}: {len(done)} done, {len(todo)} to run, {args.workers} workers')
    lock, counts, started = threading.Lock(), Counter(), time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(vultrial, args, r, spec) if spec is not None else pool.submit(classify, args, r)
                   for r in todo]
        # Written as each call finishes, so an interrupted run keeps everything already answered.
        for i, future in enumerate(cf.as_completed(futures), 1):
            result = future.result()
            result.update(condition=args.condition, model=args.model, run=name, config_hash=config_hash)
            with lock, open(out, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + '\n')
            counts[result['status']] += 1
            if i % 20 == 0 or i == len(todo):
                rate = (time.time() - started) / i
                say(f'  {len(done) + i}/{len(rows)}  {dict(counts)}  ~{rate * (len(todo) - i) / 60:.0f} min left')
    failed = sum(v for k, v in counts.items() if k.startswith('error'))
    if failed:
        say(f'{failed} calls failed; run the same command again to retry only those.')


def exact_mcnemar(gained, lost):
    n = gained + lost
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(min(gained, lost) + 1)) / 2 ** n)


def score(args):
    labels = read_rows(PREPARED / 'labels.jsonl')
    pairs = {}
    for r in labels:
        pairs.setdefault(r['pair_id'], {'dev_a': r.get('dev_a', False)})[int(r['target'])] = r['sample_id']
    runs = {}
    for path in sorted(Path(args.out).glob('predictions_*.jsonl')):
        latest = {}
        for r in read_rows(path):
            latest[r['sample_id']] = r  # a retried call overrides its earlier error row
        runs[path.stem[len('predictions_'):]] = {s: r.get('prediction') for s, r in latest.items()}
    if not runs:
        sys.exit(f'No predictions_*.jsonl in {args.out}')
    outcome = lambda p, pr: {(1, 0): 'P-C', (1, 1): 'P-V', (0, 0): 'P-B', (0, 1): 'P-R'}.get(
        (pr.get(p[1]), pr.get(p[0])), 'failed')
    report = {}
    for subset, chosen in (('all 435 pairs', list(pairs)), ('dev-A subset', [p for p, v in pairs.items() if v['dev_a']])):
        if not chosen:
            continue
        print(f'\n=== {subset} ({len(chosen)} pairs)')
        print(f"{'run':<44}{'P-C':>5}{'P-V':>5}{'P-B':>5}{'P-R':>5}{'fail':>6}{'C-R':>5}{'Acc':>8}{'F1':>8}  vs baseline")
        base = runs.get(args.baseline)
        base_pc = {p for p in chosen if outcome(pairs[p], base) == 'P-C'} if base else set()
        for name, pr in runs.items():
            c = Counter(outcome(pairs[p], pr) for p in chosen)
            tp = sum(pr.get(pairs[p][1]) == 1 for p in chosen)
            fp = sum(pr.get(pairs[p][0]) == 1 for p in chosen)
            tn = sum(pr.get(pairs[p][0]) == 0 for p in chosen)
            fn = len(chosen) - tp
            acc = (tp + tn) / (2 * len(chosen))
            f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
            vs = ''
            if base is not None and name != args.baseline:
                pc = {p for p in chosen if outcome(pairs[p], pr) == 'P-C'}
                g, l = len(pc - base_pc), len(base_pc - pc)
                vs = f'+{g}/-{l} (p={exact_mcnemar(g, l):.3f})'
            print(f"{name[:43]:<44}{c['P-C']:>5}{c['P-V']:>5}{c['P-B']:>5}{c['P-R']:>5}{c['failed']:>6}"
                  f"{c['P-C'] - c['P-R']:>5}{acc:>8.2%}{f1:>8.2%}  {vs}")
            report.setdefault(subset, {})[name] = dict(c, accuracy=round(acc, 4), f1=round(f1, 4), vs_baseline=vs)
    Path(args.out, 'score_report.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    print('\nP-C: both sides correct; C-R = P-C minus P-R; failed pairs count against every column.')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='mode', required=True)
    r = sub.add_parser('run')
    r.add_argument('--condition', choices=['direct', 'graph_diff', 'vultrial'], required=True)
    r.add_argument('--vultrial-message-role', choices=['system', 'user'], default='system',
                   help="VulTrial sends each prompt as a single 'system' message; use 'user' only if the API rejects that")
    r.add_argument('--model', required=True, help='e.g. qwen3-32b')
    r.add_argument('--api-base', default='https://dashscope.aliyuncs.com/compatible-mode/v1')
    r.add_argument('--api-key-env', default='DASHSCOPE_API_KEY')
    r.add_argument('--workers', type=int, default=8)
    r.add_argument('--max-tokens', type=int, default=8192)
    r.add_argument('--temperature', default='0', help="a number, or 'none' to omit it")
    r.add_argument('--seed', type=int, default=20261009, help='-1 = do not send a seed')
    r.add_argument('--thinking', choices=['on', 'off', 'default'], default='on',
                   help="sends enable_thinking (DashScope Qwen3); 'default' sends nothing")
    r.add_argument('--stream', choices=['auto', 'on', 'off'], default='auto', help='auto = stream when thinking is on')
    r.add_argument('--extra-body', default='', help='JSON merged into every request body')
    r.add_argument('--timeout', type=int, default=900)
    r.add_argument('--retries', type=int, default=5)
    r.add_argument('--subset', choices=['all', 'dev_a'], default='all',
                   help='dev_a = the 100 development pairs only (pilot); a later full run reuses those results')
    r.add_argument('--limit', type=int, default=0, help='first N functions only (smoke test)')
    r.add_argument('--tag', default='')
    r.add_argument('--out', default=str(HERE.parent / 'results'))
    s = sub.add_parser('score')
    s.add_argument('--out', default=str(HERE.parent / 'results'))
    s.add_argument('--baseline', required=True, help='run name, e.g. direct_qwen3-32b')
    args = p.parse_args()
    {'run': run, 'score': score}[args.mode](args)


if __name__ == '__main__':
    main()
