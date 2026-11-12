"""Connect pinned experimental runtimes to the existing installers and profiles.

The release template is the sole definition. Existing unrelated settings and
libraries are preserved; controller/frontend changes use their reversible plan.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys

COMPONENTS={'xbox360':['runtime-xenia-canary-linux'],'ps5':['sharpemu'],'switch2':['eglantine']}
FIELDS={'xbox360':{'adapter','launcher','command','args','userdata_subdir','environment','extensions',
                  'launch_inputs','runtime_id','probe_args','runtime_version','status','reason'},
        'ps5':{'runtime_options'},'switch2':{'runtime_id','runtime_options','runtime_identity','runtime_selection_path','launch_inputs','probe_args'}}


def configure(root, system_ids, *, apply=False, install=False):
    import emutool
    import emulator_controls
    requested=list(dict.fromkeys(system_ids))
    if not requested or any(s not in COMPONENTS for s in requested):
        raise ValueError('Plataformas disponíveis: '+', '.join(COMPONENTS))
    relative=Path('frontend/RetroFE/Config/profiles/host-linux.json')
    path=emulator_controls.target(root,relative)
    before=path.read_bytes();current=json.loads(before);desired=copy.deepcopy(current)
    template=json.loads((root/'tools/distribution/templates'/relative).read_text())
    for sid in requested:
        target=next(x for x in desired['collections'].values() if x['rom_subdir']==sid)
        source=next(x for x in template['collections'].values() if x['rom_subdir']==sid)
        for field in sorted(FIELDS[sid]):
            if field=='runtime_options':target.setdefault(field,{}).update(source[field])
            else:target[field]=copy.deepcopy(source[field])
    after=(json.dumps(desired,ensure_ascii=False,indent=2)+'\n').encode()
    result={'status':'planned','systems':requested,'components':[x for s in requested for x in COMPONENTS[s]],
            'profile_changes':before!=after,'gameplay_verified':False}
    if not apply:return result
    emulator_controls.assert_closed()
    if install:
        # Reuse the existing installer; no host package upgrade, build, extra
        # clone or Wine prefix is required by these official prebuilt assets.
        command=[sys.executable,'-B',str(root/'tools/distribution/setup.py'),'install',
                 '--components',*result['components'],'--jobs','1','--apply']
        subprocess.run(command,cwd=root,check=True)
    for sid in requested:
        entry=next(x for x in desired['collections'].values() if x['rom_subdir']==sid)
        choices=entry.get('runtime_options',{}) if sid!='xbox360' else {entry['runtime_id']:entry}
        chosen={'ps5':'sharpemu','switch2':'eglantine'}.get(sid,entry.get('runtime_id'))
        binary=(root/'frontend/RetroFE'/choices[chosen]['command']).resolve()
        if not binary.is_file() or not binary.is_relative_to(root/'emulator/linux'):
            raise ValueError('Instale e verifique o runtime antes de ativar o perfil: '+str(binary))
        import runtime_install
        recipes=runtime_install.load_recipes(root)['runtimes']
        if chosen in recipes:
            if runtime_install._sha256(binary)!=recipes[chosen]['source_sha256']:
                raise ValueError('Runtime diverge do SHA-256 pinado: '+chosen)
        else:
            sys.path.insert(0,str(root/'tools/distribution'))
            from setup import Installer
            engine=Installer(root,jobs=1)
            recipe=json.loads((root/'tools/distribution/config/installation.json').read_text())['components'][chosen]
            valid,reason=engine.receipt_reuse(chosen,recipe,'archive')
            if not valid:raise ValueError('Recibo do runtime não confirmado: '+reason)
    if path.read_bytes()!=before:raise RuntimeError('Perfil mudou durante a preparação; alterações preservadas')
    if before!=after:
        emutool._atomic_write(path,after,0o600)
        try:
            result['profile_operation_id']=emutool._record_file_change('experimental_runtime_profiles',
                'Integrar runtimes oficiais fixados, sem duplicar jogos/mídia nem substituir dados pessoais.',
                path,before,after,{'systems':requested,'gameplay_verified':False})
        except Exception:
            if path.read_bytes()==after:emutool._atomic_write(path,before,0o600)
            raise
    plan=emulator_controls.plan(root,system_id='xbox360',frontends=True)
    result['frontend_operation']=emulator_controls.execute(root,plan['operation_id'],'apply')
    result['verification']=emulator_controls.execute(root,plan['operation_id'],'verify')
    result['status']='configured_verified_not_gameplay_accepted'
    return result
