"""Optional GCP and capability panels inside the existing guided workbench."""
from pathlib import Path
import html
import json

def launch(project,reviewer='',on_recommendations=None):
    import ipywidgets as w
    import pandas as pd
    from IPython.display import display,HTML,clear_output
    from common import all_cards,SHARED_CACHE_ROOT
    from gcp_config import load,save
    from evidence_store import EvidenceStore,initialize_cloud,sync_sources,sync_run,search_bigquery,smoke_cloud
    from capability_assessment import make_plan,assess,save_human_decision
    from gcp_review import interactive_google_search
    import gcp_review_queue as queue_runner
    from review_workflow import current_queue_summary
    project=Path(project)
    store=EvidenceStore(project/'cache/cloud_evidence')
    config_text=w.Textarea(value=json.dumps(load(project),indent=2),layout=w.Layout(width='100%',height='360px'))
    output=w.Output()
    def callback(button,fn,out=output):
        def run(_):
            button.disabled=True
            with out:
                clear_output(wait=True)
                try:fn()
                except Exception as e:print(type(e).__name__+': '+str(e))
                finally:button.disabled=False
        button.on_click(run)
    save_button=w.Button(description='Save GCP settings')
    callback(save_button,lambda:(save(project,json.loads(config_text.value)),print('Settings saved with before/after history.')))
    settings_panel=w.VBox([w.HTML('<p>Start with five resource UIDs. Set your collector contact, bucket and dataset. '
        'Admit sources only where public-documentation caching is permitted; the default is <code>review_required</code>. '
        'Settings live in <code>inputs/gcp_settings.json</code>. GCP_ID stays in Colab Secrets.</p>'),config_text,save_button])

    collect=w.Checkbox(value=False,description='Fetch approved public sources')
    gemini=w.Checkbox(value=False,description='Use Gemini (billable)')
    preview=w.Button(description='Preview assessment plan')
    execute=w.Button(description='Run bounded assessment',button_style='info')
    callback(preview,lambda:print(json.dumps(make_plan(all_cards(),load(project)),indent=2)))
    def run_assessment():
        result=assess(project,all_cards(),load(project),collect=collect.value,use_gemini=gemini.value)
        print(json.dumps(result,indent=2));refresh_runs()
    callback(execute,run_assessment)
    run_select=w.Dropdown(options=[],description='Evidence run',layout=w.Layout(width='95%'))
    finding_select=w.Dropdown(options=[],description='Finding',layout=w.Layout(width='95%'))
    details=w.Output();decision=w.Dropdown(options=['DEFER','ACCEPT_DOCUMENTED_CLAIM','REJECT','NOT_APPLICABLE'],description='Decision')
    reason=w.Textarea(placeholder='Review rationale and remaining limitations',layout=w.Layout(width='95%'))
    save_review=w.Button(description='Save named evidence review')
    finding_map={}
    def show_finding(_=None):
        with details:
            clear_output(wait=True)
            item=finding_map.get(finding_select.value)
            if item:display(pd.DataFrame([item]).T)
    def load_findings(_=None):
        finding_map.clear()
        if run_select.value:
            from evidence_store import verify_run
            verify_run(run_select.value)
            for row in json.loads((Path(run_select.value)/'findings.json').read_text()):finding_map[row['finding_id']]=row
        finding_select.options=[(r['repo_uid']+' · '+r['criterion_id']+' · '+r['status'],key) for key,r in finding_map.items()]
        show_finding()
    def refresh_runs():
        run_select.options=[(p.name,str(p)) for p in sorted((project/'runs/capability_assessment').glob('*'),reverse=True) if (p/'manifest.json').is_file()]
        load_findings()
    run_select.observe(load_findings,names='value');finding_select.observe(show_finding,names='value')
    refresh=w.Button(description='Refresh evidence runs');callback(refresh,refresh_runs)
    callback(save_review,lambda:print('Saved:',save_human_decision(run_select.value,finding_select.value,decision.value,reviewer,reason.value,project)))
    refresh_runs()
    assessment_panel=w.VBox([w.HTML('<p>22 criteria across A–D. Cache-only and no model calls by default. '
        'Evidence reviews remain separate from identifier decisions and do not mutate cards or block normal finalization.</p>'),
        collect,gemini,w.HBox([preview,execute]),refresh,run_select,finding_select,details,decision,reason,save_review])

    queues=w.SelectMultiple(options=[],description='Queues',layout=w.Layout(width='95%',height='130px'))
    refresh_queues=w.Button(description='Refresh candidate queues')
    def queue_options():
        queues.options=[(r['label']+' · '+str(r['pending_rows'])+' pending',r['source_file']) for r in current_queue_summary()
                        if r['family'] in ('card','identifier','rrid','fairsharing','crosswalk') and r['pending_rows']]
        queues.value=tuple(value for label,value in queues.options)
    callback(refresh_queues,queue_options);queue_options()
    row_cap=w.BoundedIntText(value=10,min=1,max=5000,description='Row cap')
    batch_cap=w.BoundedIntText(value=2,min=1,max=1000,description='Calls/click')
    second_reviewer=w.Checkbox(value=load(project).get('review_second_provider')=='claude',description='Add independent Claude review (additional calls)',indent=False)
    def queue_config():
        cfg=load(project)
        cfg['review_second_provider']='claude' if second_reviewer.value else 'none'
        return cfg
    registry=w.Text(value='final_ultimate_enhanced_repos.csv' if (project/'inputs/final_ultimate_enhanced_repos.csv').is_file() else '',
                    description='Registry CSV',placeholder='Optional filename inside inputs/',layout=w.Layout(width='95%'))
    plan_select=w.Dropdown(options=[],description='Queue plan',layout=w.Layout(width='95%'))
    preview_queue=w.Button(description='Preview queue plan')
    create_queue=w.Button(description='Freeze queue plan')
    review_button=w.Button(description='Run / resume GCP review',button_style='info')
    retry=w.Checkbox(value=False,description='Retry failed/interrupted batches (may bill again)',indent=False)
    def compact(result):
        view={key:value for key,value in result.items() if key not in ('batches','selection')}
        if 'selection' in result:view['selection']=result['selection']
        print(json.dumps(view,indent=2))
        if isinstance(result.get('batches'),list):display(pd.DataFrame(result['batches']))
    def refresh_plans(select=None):
        old=select or plan_select.value
        plan_select.options=[(p.name,str(p)) for p in sorted((project/'runs/gcp_review_queue').glob('*'),reverse=True)
                             if (p/'plan.json').is_file() and (p/'manifest.json').is_file()]
        if old in {v for _,v in plan_select.options}:plan_select.value=old
    def preview_selected():
        if not queues.value:raise ValueError('Select pending queues.')
        compact(queue_runner.preview(queues.value,queue_config(),max_items=row_cap.value,registry_file=registry.value))
    def freeze_selected():
        if not queues.value:raise ValueError('Select pending queues.')
        result=queue_runner.create_plan(queues.value,queue_config(),max_items=row_cap.value,registry_file=registry.value)
        refresh_plans(result['plan_dir']);compact(result)
    callback(preview_queue,preview_selected);callback(create_queue,freeze_selected)
    refresh_plan_button=w.Button(description='Refresh saved plans')
    callback(refresh_plan_button,refresh_plans)
    show_plan=w.Button(description='Show plan progress')
    callback(show_plan,lambda:compact(queue_runner.status(plan_select.value)))
    archive_plan=w.Button(description='Archive queue plan/results to GCS')
    callback(archive_plan,lambda:print(json.dumps(queue_runner.archive_plan(plan_select.value,load(project)),indent=2)))
    def review_queue():
        if not plan_select.value:raise ValueError('Freeze or select a queue plan first.')
        result=queue_runner.run_plan(plan_select.value,max_batches=batch_cap.value,retry_failed=retry.value,progress=print)
        compact(result)
        print('Open recommendations for named human approval, then Validate and Apply all in the existing finalization panel. DEFER leaves card/name and identifier rows unresolved.')
        if on_recommendations:on_recommendations()
    callback(review_button,review_queue)
    test_claude=w.Button(description='Test Claude access')
    def smoke_claude():
        from tracker_claude_backend import smoke_test
        print(json.dumps(smoke_test(queue_config()),indent=2))
    callback(test_claude,smoke_claude)
    refresh_plans()
    batch_panel=w.VBox([w.HTML('<p>Card/name, identifier and crosswalk candidates now support Gemini review. '
        'Preview and freeze a plan without model calls. Start with 10 rows, then increase the row cap for the full backlog. '
        'Each click starts at most the selected number of model requests (five rows per batch by default). Successful imports are skipped on resume. '
        'No bucket or BigQuery setup is needed for Gemini review alone. Every card change still requires named human approval.</p>'
        '<p>The optional registry CSV supplies exact-name matched source rows, with its hash recorded. '
        'Existing permitted source-cache excerpts are retrieved by resource UID (up to three per resource), with their original dates and hashes. '
        'A missing or ambiguous source is an evidence gap; fuzzy similarity cannot establish identity. '
        'Finish drafting and approving a plan before applying card changes; otherwise create a new plan for stale batches.</p>'
        '<p>Optional Claude review uses the same sealed evidence without seeing Gemini’s recommendation. '
        'Enable the Claude model and structured outputs in your GCP project before testing. '
        'Agreement is a triage signal, not verification; all approvals remain human. The model selection is frozen with the plan.</p>'),
        refresh_queues,queues,registry,row_cap,second_reviewer,test_claude,w.HBox([preview_queue,create_queue]),refresh_plan_button,
        plan_select,show_plan,batch_cap,retry,review_button,archive_plan])

    initialize=w.Button(description='Initialize/check cloud index')
    smoke=w.Button(description='Test Gemini, GCS and BigQuery')
    callback(smoke,lambda:print(json.dumps(smoke_cloud(load(project)),indent=2)))
    sync=w.Button(description='Sync approved source cache')
    archive=w.Button(description='Archive selected evidence run')
    import_cache=w.Button(description='Reuse linked tracker snapshots')
    def import_prior():
        from tracker_cache_bridge import import_existing
        report,updated=import_existing(project,SHARED_CACHE_ROOT,all_cards(),load(project))
        save(project,updated);config_text.value=json.dumps(updated,indent=2);print(json.dumps(report,indent=2))
    callback(import_cache,import_prior)
    callback(initialize,lambda:print(json.dumps(initialize_cloud(load(project)),indent=2)))
    callback(sync,lambda:print(json.dumps(sync_sources(store,load(project)),indent=2)))
    callback(archive,lambda:print(json.dumps(sync_run(project,run_select.value,load(project)),indent=2)))
    query=w.Text(placeholder='Search captured documentation',layout=w.Layout(width='90%'))
    search_mode=w.Dropdown(options=['Drive cache','BigQuery cache'],description='Search in')
    search=w.Button(description='Search evidence')
    def search_evidence():
        cfg=load(project)
        rows=store.search(query.value) if search_mode.value=='Drive cache' else search_bigquery(cfg,query.value,max_bytes=cfg['maximum_bytes_billed'])
        display(pd.DataFrame(rows))
    callback(search,search_evidence)
    cloud_panel=w.VBox([w.HTML('<p>Explicit cloud writes only. GCS holds immutable bytes and run manifests; '
        'BigQuery is a rebuildable source index. No recursive project upload, credentials, human reviews, '
        'or Google-grounded results are included. BigQuery search has a configured bytes-billed cap.</p>'),
        import_cache,smoke,initialize,sync,archive,query,search_mode,search])

    question=w.Textarea(placeholder='Interactive public-web research question',layout=w.Layout(width='95%'))
    research=w.Button(description='Ask Google Search + Gemini',button_style='info')
    research_out=w.Output();clear_research=w.Button(description='Clear research display')
    def grounded():
        result=interactive_google_search(question.value,load(project))
        grounding=result['grounding']
        suggestions=grounding.get('searchEntryPoint',{}).get('renderedContent','')
        if not suggestions:
            print('No Search Suggestions were returned; no grounded result is displayed or stored.');return
        text='<pre style="white-space:pre-wrap">'+html.escape(result['text'])+'</pre>'
        references=[]
        for chunk in grounding.get('groundingChunks',[]):
            web=chunk.get('web',{});url=web.get('uri','')
            if url.startswith(('http://','https://')):
                references.append('<li><a target="_blank" rel="noopener noreferrer" href="'+html.escape(url,quote=True)+'">'+html.escape(web.get('title') or url)+'</a></li>')
        body=text+'<ul>'+''.join(references)+'</ul>'+suggestions
        display(HTML(body))
        # No return to a global variable and no disk, cache, source queue or BQ write.
    callback(research,grounded,research_out)
    def clear(_):research_out.clear_output();question.value=''
    clear_research.on_click(clear)
    research_panel=w.VBox([w.HTML('<p>Interactive research for the person asking. Results and Search Suggestions '
        'stay together in this display. They are not crawler seeds or assessment evidence and are never added '
        'to the durable cache. Clear the display before sharing or exporting a notebook.</p>'),question,research,clear_research,research_out])
    tabs=w.Tab(children=[settings_panel,assessment_panel,batch_panel,cloud_panel,research_panel])
    for i,title in enumerate(['Settings','A–D evidence','Gemini review','Cloud cache','Web research']):tabs.set_title(i,title)
    return w.VBox([tabs,output])
