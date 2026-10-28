import importlib.util

import pytest

from bigyear_planner.util import ROOT, dump, sha256


@pytest.mark.parametrize('corrupted',[False,True])
def test_frozen_flight_network_restores_required_original_archives(tmp_path,monkeypatch,corrupted):
    spec = importlib.util.spec_from_file_location('prepare_flights_test',ROOT/'scripts/prepare_flights.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = tmp_path/'expected.zip'
    expected.write_bytes(b'original frozen BTS archive')
    manifest = tmp_path/'manifest.json'
    dump(manifest,{'year':2023,'sources':[{'file':'month.zip','url':'https://example.invalid/source.zip',
        'sha256':sha256(expected)}]})
    before = sha256(manifest)
    def retrieve(url,path):
        assert url=='https://example.invalid/source.zip'
        path.write_bytes(b'changed source' if corrupted else expected.read_bytes())
    monkeypatch.setattr(module,'download',retrieve)
    if corrupted:
        with pytest.raises(ValueError,match='original flight-audit archive changed'):
            module.main(2023,tmp_path)
    else:
        module.main(2023,tmp_path)
        assert (tmp_path/'month.zip').read_bytes()==expected.read_bytes()
    assert sha256(manifest)==before  # Restoration never rebinds frozen evidence.
