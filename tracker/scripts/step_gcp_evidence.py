"""Optional GCP/evidence tasks. No cards or publication state are modified."""
import argparse
import json
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','assess','review-batch','review-plan','review-resume','review-status','sync-sources','sync-run','search','initialize-cloud','smoke'])
    parser.add_argument('--collect',action='store_true');parser.add_argument('--gemini',action='store_true')
    parser.add_argument('--batch');parser.add_argument('--run');parser.add_argument('--query',default='')
    parser.add_argument('--bigquery',action='store_true')
    parser.add_argument('--queue',action='append',default=[])
    parser.add_argument('--registry-file',default='')
    parser.add_argument('--max-items',type=int,default=10)
    parser.add_argument('--max-batches',type=int,default=2)
    parser.add_argument('--retry-failed',action='store_true')
    args=parser.parse_args()
    from common import PROJECT,all_cards
    from gcp_config import load
    from evidence_store import EvidenceStore,sync_sources,sync_run,search_bigquery,initialize_cloud
    from capability_assessment import make_plan,assess
    config=load(PROJECT);store=EvidenceStore(Path(PROJECT)/'cache/cloud_evidence')
    if args.action=='plan':result=make_plan(all_cards(),config)
    elif args.action=='assess':result=assess(PROJECT,all_cards(),config,collect=args.collect,use_gemini=args.gemini)
    elif args.action=='review-batch':
        if not args.batch:parser.error('--batch is required')
        from gcp_review import run_batch
        result=run_batch(args.batch,config)
    elif args.action=='review-plan':
        from gcp_review_queue import create_plan
        if not args.queue:parser.error('Specify one or more --queue paths.')
        result=create_plan(args.queue,config,max_items=args.max_items,registry_file=args.registry_file)
    elif args.action in ('review-resume','review-status'):
        if not args.run:parser.error('--run must identify a frozen queue plan.')
        from gcp_review_queue import run_plan,status
        result=run_plan(args.run,max_batches=args.max_batches,retry_failed=args.retry_failed,progress=print) if args.action=='review-resume' else status(args.run)
    elif args.action=='sync-sources':result=sync_sources(store,config)
    elif args.action=='sync-run':
        if not args.run:parser.error('--run is required')
        result=sync_run(PROJECT,args.run,config)
    elif args.action=='search':result=search_bigquery(config,args.query,max_bytes=config['maximum_bytes_billed']) if args.bigquery else store.search(args.query)
    elif args.action=='initialize-cloud':result=initialize_cloud(config)
    else:
        from tracker_gcp_backend import smoke_test
        result=smoke_test(config)
    print(json.dumps(result,indent=2,default=str))
    return 0

if __name__=='__main__':raise SystemExit(main())
