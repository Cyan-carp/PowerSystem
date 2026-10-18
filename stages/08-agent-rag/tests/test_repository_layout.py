"""Offline layout compatibility checks; never import knowledge or contact Docker/SSH."""
import ast
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def test_directory():
    # Windows managed sandboxes may reject tempfile's owner-only directory ACL.
    base=ROOT/'artifacts/repository-layout-test-tmp'
    base.mkdir(parents=True,exist_ok=True)
    directory=base/uuid4().hex
    directory.mkdir()
    try:
        yield str(directory)
    finally:
        assert directory.resolve().parent==base.resolve() and not directory.is_symlink()
        shutil.rmtree(directory)


def script(name):
    return runpy.run_path(str(ROOT/name), run_name='layout_test_only')


class RepositoryLayoutTest(unittest.TestCase):
    def test_manifest_sources_exist_and_document_identities_stay_stable(self):
        for stage in ('07-agent', '08-agent-rag'):
            entries=json.loads((ROOT/'stages'/stage/'knowledge/sources.json').read_text(encoding='utf-8'))
            for entry in entries:
                self.assertTrue((ROOT/entry['path']).is_file(), entry['path'])
                if entry['path'].startswith(('notes/03-平台使用运维与二次开发手册/', 'notes/05-设备知识设计素材与历史归档资产/设备知识/', 'notes/01-项目调研规划与开发建设记录/')):
                    self.assertIn('title', entry)
            guide=next(entry for entry in entries if entry['path']=='notes/03-平台使用运维与二次开发手册/平台使用手册.md')
            self.assertEqual(guide['title'], '平台使用手册.md')

    def test_production_document_mounts_match_the_complete_manifest(self):
        compose=ROOT/'stages/08-agent-rag/deploy/production.compose.yaml'
        mounts={}
        for host,container in re.findall(r'^\s+- ([^$\n]+):(/documents/[^\n]+):ro$', compose.read_text(encoding='utf-8'), re.M):
            source=(compose.parent/host).resolve()
            self.assertTrue(source.is_file(), host)
            self.assertTrue(source.is_relative_to(ROOT))
            mounts[container.removeprefix('/documents/')]=source
        entries=json.loads((ROOT/'stages/08-agent-rag/knowledge/sources.json').read_text(encoding='utf-8'))
        self.assertEqual(set(mounts), {entry['path'] for entry in entries})
        for entry in entries:
            self.assertEqual(mounts[entry['path']], ROOT/entry['path'])

    def test_legacy_builder_preserves_ids_across_a_physical_move(self):
        build=script('stages/07-agent/scripts/build-knowledge.py')['build']
        with test_directory() as temporary:
            root=Path(temporary)
            manifest=root/'stages/07-agent/knowledge/sources.json'
            manifest.parent.mkdir(parents=True)
            old=root/'guide.md';old.write_text('# Guide\n\n## Topic\nSame approved content.\n',encoding='utf-8')
            entry={'path':'guide.md','authority':'current','kind':'project_guide'}
            manifest.write_text(json.dumps([entry]),encoding='utf-8')
            before=build(root)
            new=root/'notes/03-平台使用运维与二次开发手册/guide.md';new.parent.mkdir(parents=True);old.rename(new)
            entry.update(path='notes/03-平台使用运维与二次开发手册/guide.md',title='guide.md')
            manifest.write_text(json.dumps([entry],ensure_ascii=False),encoding='utf-8')
            self.assertEqual(build(root),before)

    def make_server(self, root, modern):
        (root/'README.md').write_text('navigation',encoding='utf-8')
        notes=root/'notes/02-工程规范边界与验收治理' if modern else root
        notes.mkdir(parents=True,exist_ok=True)
        for name in ('项目工程规范.md','项目边界与协作红线.md','项目验收与收尾清单.md','运行产物说明.md'):
            (notes/name).write_text('approved document',encoding='utf-8')
        version=root/('notes/01-项目调研规划与开发建设记录/第二版-智能体升级' if modern else '第二版-智能体升级')
        version.mkdir(parents=True)
        (version/'index.md').write_text('approved version notes',encoding='utf-8')
        private=version/'private';private.mkdir();(private/'secret.md').write_text('fixture',encoding='utf-8')

    def test_boundary_baseline_accepts_isolated_notes_layout(self):
        collect=script('stages/08-agent-rag/deploy/boundary-release.py')['baseline_sources']
        with test_directory() as temporary:
            root=Path(temporary);self.make_server(root,True)
            rows=collect(root)
            self.assertIn('notes/02-工程规范边界与验收治理/项目工程规范.md',rows)
            self.assertIn('notes/01-项目调研规划与开发建设记录/第二版-智能体升级/index.md',rows)
            self.assertFalse(any('/private/' in row for row in rows))

    def test_boundary_baseline_rejects_missing_documents(self):
        collect=script('stages/08-agent-rag/deploy/boundary-release.py')['baseline_sources']
        with test_directory() as temporary:
            root=Path(temporary);self.make_server(root,True)
            (root/'README.md').unlink()
            with self.assertRaises(FileNotFoundError):collect(root)

    def test_boundary_candidate_image_copies_matching_importer_and_manifest(self):
        run=script('stages/08-agent-rag/deploy/boundary-release.py')['run']
        with test_directory() as temporary:
            root=Path(temporary);release=root/'runtime/layout-fixture';release.mkdir(parents=True)
            with tarfile.open(release/'candidate.tar.gz','w:gz') as out:
                for name,data in [('knowledge/sources.json',b'{}'),('scripts/knowledge.py',b'# approved fixture\n')]:
                    member=tarfile.TarInfo('stages/08-agent-rag/'+name);member.size=len(data)
                    out.addfile(member,io.BytesIO(data))
            with patch.dict(run.__globals__,{'ROOT':root}), patch.object(os,'uname',return_value=SimpleNamespace(nodename='R730XD'),create=True), patch.object(subprocess,'run') as docker:
                run(SimpleNamespace(mode='build',release=release,revision=''))
            generated=(release/'candidate/Dockerfile.agent-rag').read_text()
            self.assertIn('COPY stages/08-agent-rag/scripts /app/scripts',generated)
            self.assertIn('COPY stages/08-agent-rag/knowledge /app/knowledge',generated)
            self.assertIn('USER 10002:10002',generated)
            self.assertEqual(docker.call_count,3)  # Docker was fully mocked; no daemon contacted.

    def test_old_incremental_package_keeps_unchanged_baseline_knowledge(self):
        run=script('stages/08-agent-rag/deploy/boundary-release.py')['run']
        with test_directory() as temporary:
            root=Path(temporary);release=root/'runtime/old-fixture';release.mkdir(parents=True)
            with tarfile.open(release/'candidate.tar.gz','w:gz') as out:
                data=b'# legacy fixture';member=tarfile.TarInfo('stages/08-agent-rag/rag_agent/__init__.py');member.size=len(data)
                out.addfile(member,io.BytesIO(data))
            with patch.dict(run.__globals__,{'ROOT':root}), patch.object(os,'uname',return_value=SimpleNamespace(nodename='R730XD'),create=True), patch.object(subprocess,'run'):
                run(SimpleNamespace(mode='build',release=release,revision=''))
            generated=(release/'candidate/Dockerfile.agent-rag').read_text()
            self.assertNotIn('COPY stages/08-agent-rag/knowledge',generated)
            self.assertNotIn('COPY stages/08-agent-rag/scripts',generated)
            self.assertIn('FROM powersystem-bnd-agent-rag:baseline-old-fixture',generated)

    def test_source_sync_accepts_new_notes_but_not_runtime_or_private_files(self):
        administrative=script('stages/07-agent/deploy/sync-v2m4-source.py')['administrative']
        for name in ('notes/02-工程规范边界与验收治理/项目工程规范.md','notes/03-平台使用运维与二次开发手册/平台使用手册.md','notes/04-系统部署发布备份与变更记录/部署与运维日志/14-本地服务器部署笔记.md','notes/01-项目调研规划与开发建设记录/第二版-智能体升级/index.md'):
            self.assertTrue(administrative(PurePosixPath(name)),name)
        for name in ('notes/02-工程规范边界与验收治理/private/secret.md','artifacts/asset-archive/release.md','notes/03-平台使用运维与二次开发手册/../.env','deploy/edge-server/独立云服务器部署/config/new.sh'):
            self.assertFalse(administrative(PurePosixPath(name)),name)

    def test_packager_allowed_paths_retain_narrow_private_boundaries(self):
        general=script('stages/07-agent/scripts/v2m4-package.py')['allowed_source']
        manual=script('stages/07-agent/scripts/reference-manual-package.py')['allowed_source']
        for name in ('notes/02-工程规范边界与验收治理/项目工程规范.md','notes/03-平台使用运维与二次开发手册/平台使用手册.md','notes/01-项目调研规划与开发建设记录/第一版-平台主体/index.md','notes/01-项目调研规划与开发建设记录/第二版-智能体升级/index.md'):
            self.assertTrue(general(Path(name)),name)
        for name in ('notes/05-设备知识设计素材与历史归档资产/设备知识/source.md','notes/05-设备知识设计素材与历史归档资产/设备知识/手册来源清单.json'):
            self.assertTrue(manual(Path(name)),name)
        for allowed in (general,manual):
            for name in ('artifacts/asset-archive/archive.tar.gz','notes/02-工程规范边界与验收治理/private/secret.md','notes/05-设备知识设计素材与历史归档资产/设备知识/private/secret.json','notes/03-平台使用运维与二次开发手册/../.env'):
                self.assertFalse(allowed(Path(name)),name)
        self.assertFalse(manual(Path('notes/05-设备知识设计素材与历史归档资产/设备知识/original.pdf')))

    def test_new_note_package_is_archiveable_without_using_the_repo_index(self):
        main=script('stages/07-agent/scripts/v2m4-package.py')['main']
        names=['notes/02-工程规范边界与验收治理/项目工程规范.md','notes/03-平台使用运维与二次开发手册/平台使用手册.md','notes/01-项目调研规划与开发建设记录/第二版-智能体升级/index.md']
        raw=b'approved fixture';commit='a'*40;base='b'*40
        def git(command,**kwargs):
            self.assertEqual(command[0],'git')
            if command[1]=='rev-parse':return (base if command[2].startswith('base') else commit).encode()
            if command[1]=='diff':return ('\0'.join(names)+'\0').encode('utf-8')
            if command[1]=='show':return raw
            raise AssertionError(command)
        with test_directory() as temporary, patch.object(subprocess,'check_output',side_effect=git):
            output=Path(temporary)/'candidate'
            main(SimpleNamespace(commit='candidate',base='base',output=output))
            with tarfile.open(output/'candidate.tar.gz') as archive:self.assertEqual(archive.getnames(),sorted(names))
            report=json.loads((output/'candidate.json').read_text(encoding='utf-8'))
            self.assertTrue(all(row['sha256']==hashlib.sha256(raw).hexdigest() for row in report['files']))

    def test_release_embedded_python_allows_new_layout_and_compiles(self):
        cases={
            'v2m4-release.sh':'notes/02-工程规范边界与验收治理/项目工程规范.md',
            'manual-advice-release.sh':'notes/03-平台使用运维与二次开发手册/平台使用手册.md',
            'reference-manual-release.sh':'notes/05-设备知识设计素材与历史归档资产/设备知识/手册来源清单.json',
        }
        for filename,name in cases.items():
            raw=(ROOT/'stages/07-agent/deploy'/filename).read_text(encoding='utf-8')
            blocks=re.findall(r"<<'PY'\n(.*?)\nPY",raw,re.S)
            self.assertTrue(blocks)
            decisions=[]
            for block in blocks:
                tree=ast.parse(block)
                for node in ast.walk(tree):
                    if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='allowed' for target in node.targets):
                        decisions.append(eval(compile(ast.Expression(node.value),filename,'eval'),{'__builtins__':{'len':len}}, {'member':SimpleNamespace(name=name),'path':PurePosixPath(name)}))
            self.assertEqual(decisions,[True],filename)
            self.assertIn("'private'",raw)

    def test_manual_manifest_selection_uses_isolated_notes_layout(self):
        expected=PurePosixPath('notes/05-设备知识设计素材与历史归档资产/设备知识/手册来源清单.json')
        cases=[('stages/07-agent/deploy/install-reference-manual.sh','manifest'),
               ('stages/04-frontend/deploy/backup.sh','manuals_manifest')]
        for name,variable in cases:
            raw=(ROOT/name).read_text(encoding='utf-8')
            match=re.search(rf'(?m)^\s*{variable}="\$project_dir/([^\n"]+)"$',raw)
            self.assertIsNotNone(match,name)
            self.assertEqual(PurePosixPath(match.group(1)),expected,name)
            with test_directory() as temporary:
                root=Path(temporary)
                selected=root/Path(*expected.parts)
                selected.parent.mkdir(parents=True)
                selected.write_text('{}',encoding='utf-8')
                self.assertTrue(selected.is_file())

    def test_server_runtime_paths_are_not_redirected_to_asset_archives(self):
        for name in ('stages/04-frontend/deploy/backup.sh','stages/07-agent/deploy/install-reference-manual.sh'):
            raw=(ROOT/name).read_text(encoding='utf-8')
            self.assertIn('$project_dir/runtime/',raw)
            self.assertNotIn('$project_dir/artifacts/asset-archive/',raw)
        source=(ROOT/'stages/08-agent-rag/deploy/boundary-release.py').read_text(encoding='utf-8')
        self.assertIn("r.is_relative_to(ROOT/'runtime')",source)


if __name__=='__main__':unittest.main()
