import unittest
from unittest import mock

from huggingface_hub.utils import EntryNotFoundError, LocalEntryNotFoundError

import hf_dataset_store


class DownloadTextFileTest(unittest.TestCase):
    # Si un error de red se toma como "no existe", load_index devuelve [] y save_index pisa el índice.
    def test_sin_red_propaga_el_error(self):
        with mock.patch.object(hf_dataset_store, "hf_hub_download", side_effect=LocalEntryNotFoundError("sin red")):
            with self.assertRaises(LocalEntryNotFoundError):
                hf_dataset_store.download_text_file("x/y", "index/index.json")

    def test_archivo_inexistente_devuelve_none(self):
        with mock.patch.object(hf_dataset_store, "hf_hub_download", side_effect=EntryNotFoundError("404")):
            self.assertIsNone(hf_dataset_store.download_text_file("x/y", "index/index.json"))


if __name__ == "__main__":
    unittest.main()
