"""Modeling routes inherit the project's authentication and membership checks."""
import json
import zipfile

from fastapi import File, Form, UploadFile, Query
from fastapi.responses import FileResponse
from .modeling_models import DatasetRequest, DataMapping, StudyRequest, CandidateRequest, Budget, FinishStudy
from .training_notes import render_note, write_bundle


def register_modeling_routes(router, services, invoke):
    modeling = services.modeling

    @router.get('/datasets')
    async def datasets(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), summary: bool = False, after: str = ''):
        return await modeling.list(project_id, 'dataset', offset=offset, limit=limit, summary=summary, after=after)

    @router.post('/datasets', status_code=201)
    async def register(project_id: str, body: DatasetRequest):
        return await invoke(modeling.dataset, project_id, body)

    @router.post('/datasets/upload', status_code=201)
    async def upload(project_id: str, file: UploadFile = File(...), mapping: str = Form('{}')):
        async def create():
            content = await file.read(100 * 1024 * 1024 + 1)
            if len(content) > 100 * 1024 * 1024:
                raise ValueError('上传上限为 100 MB；更大文件请从项目资料登记')
            body = DatasetRequest(source_path=file.filename or 'data.csv', mapping=DataMapping.model_validate(json.loads(mapping)))
            return await modeling.dataset(project_id, body, upload=content)
        return await invoke(create)

    @router.get('/datasets/{dataset_id}')
    async def dataset(project_id: str, dataset_id: str):
        return await invoke(modeling.get, project_id, 'dataset', dataset_id)

    @router.post('/datasets/{dataset_id}/profile')
    async def profile(project_id: str, dataset_id: str, sampled: bool = False):
        return await invoke(modeling.profile, project_id, dataset_id, sampled)

    @router.get('/modeling/studies')
    async def studies(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), summary: bool = False, after: str = ''):
        return await modeling.list(project_id, 'study', offset=offset, limit=limit, summary=summary, after=after)

    @router.post('/modeling/studies', status_code=201)
    async def study(project_id: str, body: StudyRequest):
        return await invoke(modeling.study, project_id, body)

    @router.get('/modeling/studies/{study_id}')
    async def read_study(project_id: str, study_id: str):
        return await invoke(modeling.get, project_id, 'study', study_id)

    @router.get('/modeling/studies/{study_id}/next-step')
    async def next_step(project_id: str, study_id: str):
        return await invoke(modeling.next_step, project_id, study_id)

    @router.patch('/modeling/studies/{study_id}/budget')
    async def budget(project_id: str, study_id: str, body: Budget):
        return await invoke(modeling.budget, project_id, study_id, body)

    @router.post('/modeling/studies/{study_id}/finish')
    async def finish(project_id: str, study_id: str, body: FinishStudy):
        return await invoke(modeling.finish, project_id, study_id, body.reason)

    @router.get('/modeling/studies/{study_id}/candidates')
    async def candidates(project_id: str, study_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), summary: bool = False, after: str = ''):
        await invoke(modeling.get, project_id, 'study', study_id)
        return await modeling.list(project_id, 'candidate', study_id, offset, limit, summary, after)

    @router.post('/modeling/studies/{study_id}/candidates', status_code=201)
    async def candidate(project_id: str, study_id: str, body: CandidateRequest):
        return await invoke(modeling.candidate, project_id, study_id, body)

    @router.get('/modeling/studies/{study_id}/candidates/{candidate_id}')
    async def read_candidate(project_id: str, study_id: str, candidate_id: str):
        async def read():
            value = await modeling.get(project_id, 'candidate', candidate_id)
            if value['study_id'] != study_id:
                raise KeyError('候选不属于当前研究')
            return value
        return await invoke(read)

    @router.get('/modeling/studies/{study_id}/candidates/{candidate_id}/trials/{slot}/note')
    async def training_note(project_id: str, study_id: str, candidate_id: str, slot: int):
        note = await invoke(modeling.training_note, project_id, study_id, candidate_id, slot)
        return {'note': note, 'markdown': render_note(note)}

    @router.get('/modeling/studies/{study_id}/candidates/{candidate_id}/trials/{slot}/note/download')
    async def download_note(project_id: str, study_id: str, candidate_id: str, slot: int):
        async def bundle():
            note = await modeling.training_note(project_id, study_id, candidate_id, slot)
            folder = modeling.path(project_id, candidate_id)
            trial_folder = folder / 'output' / f'trial-{slot}'
            from uuid import uuid4
            archive = folder / f'note-{slot}-{uuid4()}.zip'
            from starlette.background import BackgroundTask
            environment = await modeling.snapshot_environment(note['image'])
            try:
                with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
                    write_bundle(z, note)
                    for name in ('result.json', 'validation-predictions.csv', 'preprocessing.json', 'trial-input.json', 'attempt.json', 'trial.log', 'autogluon-models.json'):
                        path = trial_folder / name
                        if path.is_file() and not path.is_symlink():
                            z.write(path, name)
                    for name in ('worker.py', 'requirements.lock', 'environment.json'):
                        z.write(environment / name, name)
                    if (environment / 'modeling_preprocessing.py').is_file():
                        z.write(environment / 'modeling_preprocessing.py', 'modeling_preprocessing.py')
                    if (folder / 'transformer.py').is_file():
                        z.write(folder / 'transformer.py', 'transformer.py')
                    if note['test_result']:
                        path = modeling.path(project_id, study_id) / 'holdout/output/holdout-predictions.csv'
                        if path.is_file():
                            z.write(path, 'holdout-predictions.csv')
            except BaseException:
                archive.unlink(missing_ok=True)
                raise
            return FileResponse(archive, media_type='application/zip', filename=f'training-note-{slot + 1}.zip', background=BackgroundTask(archive.unlink, missing_ok=True))
        return await invoke(bundle)

    @router.get('/modeling/studies/{study_id}/candidates/{candidate_id}/download')
    async def download(project_id: str, study_id: str, candidate_id: str, slot: int | None = Query(None, ge=0)):
        from .modeling_exports import model_bundle
        archive, _ = await invoke(model_bundle, modeling, project_id, study_id, candidate_id, slot)
        return FileResponse(archive, media_type='application/zip', filename='prediction-model.zip')

    @router.get('/datasets/{dataset_id}/files/{file_path:path}')
    async def file(project_id: str, dataset_id: str, file_path: str):
        async def read():
            await modeling.get(project_id, 'dataset', dataset_id)
            root = modeling.path(project_id, dataset_id).resolve()
            path = (root / file_path).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
                raise KeyError('文件不存在')
            return FileResponse(path, filename=path.name)
        return await invoke(read)
