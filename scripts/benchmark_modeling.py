"""Reproducible CPU-worker benchmark on explicitly synthetic data.

python scripts/benchmark_modeling.py --output /tmp/modeling-benchmark.json
No agent calls, no industrial data, and no access to the platform database.
"""
import argparse
import csv
import json
from pathlib import Path
import random
import subprocess
import shutil
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='lilies-modeling:20260914')
    parser.add_argument('--output', default='/tmp/modeling-benchmark.json')
    parser.add_argument('--data', help='Optional authorized tabular CSV; only reads this file')
    parser.add_argument('--target', default='target')
    parser.add_argument('--id-column', default='id')
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--maximum-trials', type=int, default=3)
    args = parser.parse_args()
    image = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.image], text=True).strip()
    report = {'type': 'supplied_data_benchmark' if args.data else 'synthetic_platform_benchmark', 'industrial_acceptance': False, 'image': image,
              'resources': {'cpus': 4, 'memory': '4g', 'network': 'none'}, 'budget_per_strategy': {'seconds': args.seconds, 'maximum_trials': args.maximum_trials}, 'results': []}
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=output_path.stem + '-records-', dir=output_path.parent))
    report['records_directory'] = str(root)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    rng = random.Random(42)
    with (root / 'table.csv').open('w') as file:
        writer = csv.writer(file); writer.writerow(['id', 'x', 'category', 'target'])
        for i in range(240):
            x = rng.uniform(-3, 3)
            writer.writerow([f'{i:04d}', x, 'A' if i % 2 else 'B', 2*x*x + (i % 2) + rng.gauss(0, .1)])
    with (root / 'series.csv').open('w') as file, (root / 'labels.csv').open('w') as label_file:
        writer = csv.writer(file); writer.writerow(['rod', 'time', 'temperature'])
        labels = csv.writer(label_file); labels.writerow(['rod', 'cutoff', 'target'])
        for i in range(50):
            level = rng.uniform(2, 10)
            for j in range(20):
                writer.writerow([f'{i:03d}', f'2026-01-01T00:00:{j:02d}Z', level + j * .1 + rng.gauss(0, .1)])
            labels.writerow([f'{i:03d}', '2026-01-01T00:00:19Z', level * 2 + rng.gauss(0, .1)])

    def run(name, config, seconds=120):
        output = root / name; output.mkdir()
        config = {**config, 'output': '/output'}
        job = root / (name + '.json'); job.write_text(json.dumps(config))
        begin = time.monotonic()
        process = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--cpus', '4', '--memory', '4g', '--pids-limit', '128', '--read-only', '--tmpfs', '/tmp:rw,size=512m',
            '-e', 'HOME=/tmp', '-v', f'{root}:/data:ro', '-v', f'{job}:/job.json:ro', '-v', f'{output}:/output:rw', image, '/job.json'], capture_output=True, text=True, timeout=seconds)
        (output / 'events.jsonl').write_text(process.stdout)
        (output / 'stderr.log').write_text(process.stderr)
        events = []
        for line in process.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        final = next((e for e in reversed(events) if e.get('kind') == 'result'), None)
        if process.returncode or not final:
            raise RuntimeError(process.stderr[-1500:] + process.stdout[-1500:])
        return final, time.monotonic() - begin

    if args.data:
        shutil.copyfile(args.data, root / 'table.csv')
    for kind in (('table',) if args.data else ('table', 'timeseries')):
        mapping = {'kind': 'tabular', 'id_column': args.id_column, 'target': args.target} if kind == 'table' else {'kind': 'timeseries', 'id_column': 'rod', 'target': 'target', 'time_column': 'time', 'prediction_time_column': 'cutoff'}
        data = {'source': '/data/table.csv' if kind == 'table' else '/data/series.csv', 'labels': '' if kind == 'table' else '/data/labels.csv', 'mapping': mapping}
        evaluation = {'problem': 'regression', 'metric': 'mae', 'split': 'random' if kind == 'table' else 'group', 'seed': 42, 'folds': 3, 'holdout_fraction': .2}
        profile, profile_wall = run(kind + '-profile', {**data, 'action': 'profile'})
        _, split_wall = run(kind + '-split', {**data, 'action': 'prepare', 'evaluation': evaluation})
        for engine in ('sklearn', 'optuna', 'autogluon'):
            result, wall = run(kind + '-' + engine, {**data, 'action': 'train', 'evaluation': evaluation, 'features': {},
                'split': '/data/' + kind + '-split/split.json', 'engine': engine, 'models': ['linear', 'forest', 'hist_gradient'],
                'parameters': {}, 'batch_size': 1 if engine == 'autogluon' else min(3, args.maximum_trials), 'trial_seconds': args.seconds if engine == 'autogluon' else args.seconds // min(3, args.maximum_trials), 'remaining_seconds': args.seconds}, seconds=args.seconds+30)
            best = result['result']['best']
            report['results'].append({'data': kind, 'engine': engine, 'profile_wall_seconds': profile_wall, 'split_wall_seconds': split_wall,
                'batch_wall_seconds': wall, 'worker_seconds': result['seconds'], 'peak_memory_bytes': result['peak_memory_bytes'],
                'mae': best['metrics']['mae'] if best else None, 'baseline': best['baseline']['mae'] if best else None,
                'best_model': best['model'] if best else None, 'best_slot': best['slot'] if best else None,
                'records_directory': str(root / (kind + '-' + engine)), 'trials': result['result']['trials'],
                'cold_image_pull_included': False, 'agent_and_ui_time_included': False,
                'fixed_split': json.loads((root / (kind + '-split/split.json')).read_text())})
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(kind, engine, 'MAE', best['metrics']['mae'] if best else 'failed', f'{wall:.2f}s', flush=True)
    print('Report:', args.output)


if __name__ == '__main__':
    main()
