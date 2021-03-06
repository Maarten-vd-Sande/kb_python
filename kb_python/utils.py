import concurrent.futures
import functools
import gzip
import logging
import os
import re
import requests
import shutil
import subprocess as sp
import threading
import time
from urllib.request import urlretrieve

import anndata
import pandas as pd
import scipy.io
from tqdm import tqdm

from .config import (
    CHUNK_SIZE,
    get_bustools_binary_path,
    get_kallisto_binary_path,
    is_dry,
    PACKAGE_PATH,
    PLATFORM,
    TECHNOLOGIES_MAPPING,
    WHITELIST_DIR,
    UnsupportedOSException,
)
from .dry import utils as dry_utils

logger = logging.getLogger(__name__)

TECHNOLOGY_PARSER = re.compile(r'^(?P<name>\S+)')
VERSION_PARSER = re.compile(r'^\S*? ([0-9]+).([0-9]+).([0-9]+)')


class NotImplementedException(Exception):
    pass


class UnmetDependencyException(Exception):
    pass


class TqdmLoggingHandler(logging.Handler):
    """Custom logging handler so that logging does not affect progress bars.
    """

    def __init__(self, level=logging.NOTSET):
        super().__init__(level)

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg)
            self.flush()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self.handleError(record)


def dryable(dry_func):
    """Function decorator to set a function as dryable.

    When this decorator is applied, the provided `dry_func` will be called
    instead of the actual function when the current run is a dry run.

    :param dry_func: function to call when it is a dry run
    :type dry_func: function

    :return: wrapped function
    :rtype: function
    """

    def wrapper(func):

        @functools.wraps(func)
        def inner(*args, **kwargs):
            if not is_dry():
                return func(*args, **kwargs)
            else:
                return dry_func(*args, **kwargs)

        return inner

    return wrapper


def open_as_text(path, mode):
    """Open a textfile or gzip file in text mode.

    :param path: path to textfile or gzip
    :type path: str
    :param mode: mode to open the file, either `w` for write or `r` for read
    :type mode: str

    :return: file object
    :rtype: file object
    """
    return gzip.open(path, mode +
                     't') if path.endswith('.gz') else open(path, mode)


def decompress_gzip(gzip_path, out_path):
    """Decompress a gzip file to provided file path.

    :param gzip_path: path to gzip file
    :type gzip_path: str
    :param out_path: path to decompressed file
    :type out_path: str

    :return: path to decompressed file
    :rtype: str
    """
    with gzip.open(gzip_path, 'rb') as f, open(out_path, 'wb') as out:
        shutil.copyfileobj(f, out)
    return out_path


def compress_gzip(file_path, out_path):
    """Compress a file into gzip.

    :param file_path: path to file
    :type file_path: str
    :param out_dir: path to compressed file
    :type out_dir: str

    :return: path to compressed file
    :rtype: str
    """
    with open(file_path, 'rb') as f, gzip.open(out_path, 'wb') as out:
        shutil.copyfileobj(f, out)
    return out_path


@dryable(dry_utils.make_directory)
def make_directory(path):
    """Quietly make the specified directory (and any subdirectories).

    This function is a wrapper around os.makedirs. It is used so that
    the appropriate mkdir command can be printed for dry runs.

    :param path: path to directory to make
    :type path: str
    """
    os.makedirs(path, exist_ok=True)


@dryable(dry_utils.remove_directory)
def remove_directory(path):
    """Quietly make the specified directory (and any subdirectories).

    This function is a wrapper around shutil.rmtree. It is used so that
    the appropriate rm command can be printed for dry runs.

    :param path: path to directory to remove
    :type path: str
    """
    shutil.rmtree(path, ignore_errors=True)


@dryable(dry_utils.run_executable)
def run_executable(
    command,
    stdin=None,
    stdout=sp.PIPE,
    stderr=sp.PIPE,
    wait=True,
    stream=True,
    quiet=False,
    returncode=0,
    alias=True,
):
    """Execute a single shell command.

    :param command: a list representing a single shell command
    :type command: list
    :param stdin: object to pass into the `stdin` argument for `subprocess.Popen`,
                  defaults to `None`
    :type stdin: stream, optional
    :param stdout: object to pass into the `stdout` argument for `subprocess.Popen`,
                  defaults to `subprocess.PIPE`
    :type stdout: stream, optional
    :param stderr: object to pass into the `stderr` argument for `subprocess.Popen`,
                  defaults to `subprocess.PIPE`
    :type stderr: stream, optional
    :param wait: whether to wait until the command has finished, defaults to `True`
    :type wait: bool, optional
    :param stream: whether to stream the output to the command line, defaults to `True`
    :type stream: bool, optional
    :param quiet: whether to not display anything to the command line and not check the return code,
                  defaults to `False`
    :type quiet: bool, optional
    :param returncode: the return code expected if the command runs as intended,
                       defaults to `0`
    :type returncode: int, optional
    :param alias: whether to use the basename of the first element of `command`,
                  defaults to `True`
    :type alias: bool, optional

    :return: the spawned process
    :rtype: subprocess.Process
    """
    command = [str(c) for c in command]
    if not quiet:
        c = command.copy()
        if alias:
            c[0] = os.path.basename(c[0])
        logger.debug(' '.join(c))
    p = sp.Popen(
        command,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        universal_newlines=wait,
        bufsize=1 if wait else -1,
    )

    # Wait if desired.
    if wait:
        out = []
        while p.poll() is None:
            if stream and not quiet:
                for line in p.stdout:
                    out.append(line.strip())
                    logger.debug(line.strip())
                for line in p.stderr:
                    out.append(line.strip())
                    logger.debug(line.strip())
            else:
                time.sleep(1)

        if not quiet and p.returncode != returncode:
            logger.error('\n'.join(out))
            raise sp.CalledProcessError(p.returncode, ' '.join(command))

    return p


def run_chain(*commands, stdin=None, stdout=sp.PIPE, wait=True, stream=False):
    """Execute multiple shell commands by piping the output into inputs.

    :param commands: lists of shell commands
    :type commands: list
    :param stdin: object to pass into the `stdin` argument for `subprocess.Popen`,
                  defaults to `None`
    :type stdin: stream, optional
    :param stdout: object to pass into the `stdout` argument for `subprocess.Popen`,
                  defaults to `subprocess.PIPE`
    :type stdout: stream, optional
    :param wait: whether to wait until the command has finished, defaults to `True`
    :type wait: bool, optional
    :param stream: whether to stream the output to the command line, defaults to `True`
    :type stream: bool, optional

    :return: list of spawned subprocesses
    :rtype: list
    """
    assert len(commands) > 1
    processes = []
    for command in commands:
        _stdin = stdin
        _stdout = stdout
        _wait = wait
        _stream = stream
        if processes:
            _stdin = processes[-1].stdout
        if len(processes) != len(commands) - 1:
            _stdout = sp.PIPE
            _wait = False
            _stream = False
        p = run_executable(
            command, stdin=_stdin, stdout=_stdout, wait=_wait, stream=_stream
        )
        processes.append(p)

        if _stdin:
            _stdin.close()

    if wait:
        for p in processes:
            while p.poll() is None:
                time.sleep(1)
            if p.returncode != 0:
                raise sp.CalledProcessError(p.returncode, ' '.join(command))
    return processes


def get_kallisto_version():
    """Get the provided Kallisto version.

    This function parses the help text by executing the included Kallisto binary.

    :return: tuple of major, minor, patch versions
    :rtype: tuple
    """
    p = run_executable([get_kallisto_binary_path()], quiet=True, returncode=1)
    match = VERSION_PARSER.match(p.stdout.read())
    return tuple(int(ver) for ver in match.groups()) if match else None


def get_bustools_version():
    """Get the provided Bustools version.

    This function parses the help text by executing the included Bustools binary.

    :return: tuple of major, minor, patch versions
    :rtype: tuple
    """
    p = run_executable([get_bustools_binary_path()], quiet=True, returncode=1)
    match = VERSION_PARSER.match(p.stdout.read())
    return tuple(int(ver) for ver in match.groups()) if match else None


def parse_technologies(lines):
    """Parse a list of strings into a list of supported technologies.

    This function parses the technologies printed by running `kallisto bus --list`.

    :param lines: the output of `kallisto bus --list` split into lines
    :type lines: list

    :return: list of technologies
    :rtype: list
    """
    parsing = False
    technologies = set()
    for line in lines:
        if line.startswith('-'):
            parsing = True
            continue

        if parsing:
            if line.isspace():
                break
            match = TECHNOLOGY_PARSER.match(line)
            if match:
                technologies.add(match['name'])
    return technologies


def get_supported_technologies():
    """Runs 'kallisto bus --list' to fetch a list of supported technologies.

    :return: list of technologies
    :rtype: list
    """
    p = run_executable([get_kallisto_binary_path(), 'bus', '--list'],
                       quiet=True,
                       returncode=1)
    return parse_technologies(p.stdout)


def whitelist_provided(technology):
    """Determine whether or not the whitelist for a technology is provided.

    :param technology: the name of the technology
    :type technology: str

    :return: whether the whitelist is provided
    :rtype: bool
    """
    upper = technology.upper()
    return upper in TECHNOLOGIES_MAPPING and TECHNOLOGIES_MAPPING[
        upper].whitelist_archive


@dryable(dry_utils.copy_whitelist)
def copy_whitelist(technology, out_dir):
    """Copies provided whitelist for specified technology.

    :param technology: the name of the technology
    :type technology: str
    :param out_dir: directory to put the whitelist
    :type out_dir: str

    :return: path to whitelist
    :rtype: str
    """
    technology = TECHNOLOGIES_MAPPING[technology.upper()]
    archive_path = os.path.join(
        PACKAGE_PATH, WHITELIST_DIR, technology.whitelist_archive
    )
    whitelist_path = os.path.join(
        out_dir,
        os.path.splitext(technology.whitelist_archive)[0]
    )
    with open_as_text(archive_path, 'r') as f, open(whitelist_path, 'w') as out:
        out.write(f.read())
    return whitelist_path


def concatenate_files(*paths, out_path, temp_dir='tmp'):
    """Concatenates an arbitrary number of files into one TEXT file.

    Only supports text and gzip files.

    :param paths: an arbitrary number of paths to files
    :type paths: str
    :param out_path: path to place concatenated file
    :type out_path: str
    :param temp_dir: temporary directory, defaults to `tmp`
    :type temp_dir: str, optional

    :return: path to concatenated file
    :rtype: str
    """
    with open(out_path, 'w') as out:
        for path in paths:
            with open_as_text(path, 'r') as f:
                for line in f:
                    if not line.isspace():
                        out.write(line.strip() + '\n')

    return out_path


def download_file(url, path):
    """Download a remote file to the provided path while displaying a progress bar.

    :param url: remote url
    :type url: str
    :param path: local path to download the file to
    :type path: str

    :return: path to downloaded file
    :rtype: str
    """
    r = requests.get(url, stream=True)
    with open(path, 'wb') as f:
        t = tqdm(
            unit='B',
            total=int(r.headers['Content-Length']),
            unit_divisor=1024,
            unit_scale=True,
            ascii=True,
        )
        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                t.update(len(chunk))
                f.write(chunk)
        t.close()
    return path


@dryable(dry_utils.stream_file)
def stream_file(url, path):
    """Creates a FIFO file to use for piping remote files into processes.

    This function spawns a new thread to download the remote file into a FIFO
    file object. FIFO file objects are only supported on unix systems.

    :param url: url to the file
    :type url: str
    :param path: path to place FIFO file
    :type path: str

    :raises UnsupportedOSException: if the OS is Windows

    :return: path to FIFO file
    :rtype: str
    """
    # Windows does not support FIFO files.
    if PLATFORM == 'windows':
        raise UnsupportedOSException((
            'Windows does not support piping remote files.'
            'Please download the file manually.'
        ))
    else:
        logger.info('Piping {} to {}'.format(url, path))
        os.mkfifo(path)
        t = threading.Thread(target=urlretrieve, args=(url, path), daemon=True)
        t.start()
    return path


def import_tcc_matrix_as_anndata(
    matrix_path, barcodes_path, ec_path, txnames_path, threads=8
):
    """Import a TCC matrix as an Anndata object.

    :param matrix_path: path to the matrix ec file
    :type matrix_path: str
    :param barcodes_path: path to the barcodes txt file
    :type barcodes_path: str
    :param genes_path: path to the ec txt file
    :type genes_path: str
    :param txnames_path: path to transcripts.txt generated by `kallisto bus`
    :type txnames_path: str

    :return: a new Anndata object
    :rtype: anndata.Anndata
    """
    df_barcodes = pd.read_csv(
        barcodes_path, index_col=0, header=None, names=['barcode']
    )
    df_ec = pd.read_csv(
        ec_path,
        index_col=0,
        header=None,
        names=['ec', 'transcripts'],
        sep='\t',
        dtype=str
    )
    df_ec.index = df_ec.index.astype(str)  # To prevent logging from anndata
    with open(txnames_path, 'r') as f:
        transcripts = [
            line.strip() for line in f.readlines() if not line.strip().isspace()
        ]

    ts = list(df_ec.transcripts)
    get_transcript_ids = lambda ts, transcripts: [
        [transcripts[int(i)] for i in t.split(',')] for t in ts
    ]
    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        chunk = int(len(ts) / threads) + 1
        for i in range(threads):
            future = executor.submit(
                get_transcript_ids, ts[i * chunk:(i + 1) * chunk], transcripts
            )
            futures.append(future)
    transcript_ids = []
    for future in futures:
        transcript_ids += future.result()
    df_ec['transcript_ids'] = transcript_ids
    df_ec.drop('transcripts', axis=1, inplace=True)
    return anndata.AnnData(
        X=scipy.io.mmread(matrix_path).tocsr(), obs=df_barcodes, var=df_ec
    )


def import_matrix_as_anndata(matrix_path, barcodes_path, genes_path):
    """Import a matrix as an Anndata object.

    :param matrix_path: path to the matrix ec file
    :type matrix_path: str
    :param barcodes_path: path to the barcodes txt file
    :type barcodes_path: str
    :param genes_path: path to the genes txt file
    :type genes_path: str

    :return: a new Anndata object
    :rtype: anndata.Anndata
    """
    df_barcodes = pd.read_csv(
        barcodes_path, index_col=0, header=None, names=['barcode']
    )
    df_genes = pd.read_csv(
        genes_path, header=None, index_col=0, names=['gene_id'], sep='\t'
    )
    return anndata.AnnData(
        X=scipy.io.mmread(matrix_path).tocsr(), obs=df_barcodes, var=df_genes
    )


def overlay_anndatas(adata_spliced, adata_unspliced):
    """'Overlays' anndata objects by taking the intersection of the obs and var
    of each anndata.

    :param adata_spliced: an Anndata object
    :type adata_spliced: anndata.Anndata
    :param adata_unspliced: an Anndata object
    :type adata_unspliced: anndata.Anndata

    :return: a new Anndata object
    :rtype: anndata.Anndata
    """
    obs_idx = adata_spliced.obs.index.intersection(adata_unspliced.obs.index)
    var_idx = adata_spliced.var.index.intersection(adata_unspliced.var.index)
    spliced_intersection = adata_spliced[obs_idx][:, var_idx]
    unspliced_intersection = adata_unspliced[obs_idx][:, var_idx]
    spliced_unspliced = spliced_intersection.copy()
    spliced_unspliced.layers['spliced'] = spliced_intersection.X
    spliced_unspliced.layers['unspliced'] = unspliced_intersection.X
    return spliced_unspliced


def sum_anndatas(adata_spliced, adata_unspliced):
    """Sum the counts in two anndata objects by taking the intersection of
    both matrices and adding the values together.

    :param adata_spliced: an Anndata object
    :type adata_spliced: anndata.Anndata
    :param adata_unspliced: an Anndata object
    :type adata_unspliced: anndata.Anndata

    :return: a new Anndata object
    :rtype: anndata.Anndata
    """
    obs_idx = adata_spliced.obs.index.intersection(adata_unspliced.obs.index)
    var_idx = adata_spliced.var.index.intersection(adata_unspliced.var.index)
    spliced_intersection = adata_spliced[obs_idx][:, var_idx]
    unspliced_intersection = adata_unspliced[obs_idx][:, var_idx]
    spliced_unspliced = spliced_intersection.copy()
    spliced_unspliced.X = spliced_intersection.X + unspliced_intersection.X
    return spliced_unspliced
