# Copyright (c) Alibaba, Inc. and its affiliates.

import copy
import io
import os
import urllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Dict, Optional, Union

import requests
from requests.adapters import Retry
from tqdm import tqdm

from modelscope.hub.api import HubApi, ModelScopeConfig
from modelscope.hub.constants import (
    API_FILE_DOWNLOAD_CHUNK_SIZE, API_FILE_DOWNLOAD_RETRY_TIMES,
    API_FILE_DOWNLOAD_TIMEOUT, FILE_HASH, MODELSCOPE_DOWNLOAD_PARALLELS,
    MODELSCOPE_PARALLEL_DOWNLOAD_THRESHOLD_MB, TEMPORARY_FOLDER_NAME)
from modelscope.utils.constant import DEFAULT_MODEL_REVISION
from modelscope.utils.file_utils import get_model_cache_root
from modelscope.utils.logger import get_logger
from .errors import NotExistError
from .utils.caching import ModelFileSystemCache
from .utils.utils import (file_integrity_validation, get_endpoint,
                          model_id_to_group_owner_name)

logger = get_logger()


def model_file_download(
    model_id: str,
    file_path: str,
    revision: Optional[str] = DEFAULT_MODEL_REVISION,
    cache_dir: Optional[str] = None,
    user_agent: Union[Dict, str, None] = None,
    local_files_only: Optional[bool] = False,
    cookies: Optional[CookieJar] = None,
    local_dir: Optional[str] = None,
) -> Optional[str]:  # pragma: no cover
    """Download from a given URL and cache it if it's not already present in the local cache.

    Given a URL, this function looks for the corresponding file in the local
    cache. If it's not there, download it. Then return the path to the cached
    file.

    Args:
        model_id (str): The model to whom the file to be downloaded belongs.
        file_path(str): Path of the file to be downloaded, relative to the root of model repo.
        revision(str, optional): revision of the model file to be downloaded.
            Can be any of a branch, tag or commit hash.
        cache_dir (str, Path, optional): Path to the folder where cached files are stored.
        user_agent (dict, str, optional): The user-agent info in the form of a dictionary or a string.
        local_files_only (bool, optional):  If `True`, avoid downloading the file and return the path to the
            local cached file if it exists. if `False`, download the file anyway even it exists.
        cookies (CookieJar, optional): The cookie of download request.
        local_dir (str, optional): Specific local directory path to which the file will be downloaded.

    Returns:
        string: string of local file or if networking is off, last version of
        file cached on disk.

    Raises:
        NotExistError: The file is not exist.
        ValueError: The request parameter error.

    Note:
        Raises the following errors:

            - [`EnvironmentError`](https://docs.python.org/3/library/exceptions.html#EnvironmentError)
            if `use_auth_token=True` and the token cannot be found.
            - [`OSError`](https://docs.python.org/3/library/exceptions.html#OSError)
            if ETag cannot be determined.
            - [`ValueError`](https://docs.python.org/3/library/exceptions.html#ValueError)
            if some parameter value is invalid
    """
    temporary_cache_dir, cache = create_temporary_directory_and_cache(
        model_id, local_dir, cache_dir)

    # if local_files_only is `True` and the file already exists in cached_path
    # return the cached path
    if local_files_only:
        cached_file_path = cache.get_file_by_path(file_path)
        if cached_file_path is not None:
            logger.warning(
                "File exists in local cache, but we're not sure it's up to date"
            )
            return cached_file_path
        else:
            raise ValueError(
                'Cannot find the requested files in the cached path and outgoing'
                ' traffic has been disabled. To enable model look-ups and downloads'
                " online, set 'local_files_only' to False.")

    _api = HubApi()
    headers = {
        'user-agent': ModelScopeConfig.get_user_agent(user_agent=user_agent, )
    }
    if cookies is None:
        cookies = ModelScopeConfig.get_cookies()

    revision = _api.get_valid_revision(
        model_id, revision=revision, cookies=cookies)
    file_to_download_info = None
    # we need to confirm the version is up-to-date
    # we need to get the file list to check if the latest version is cached, if so return, otherwise download
    model_files = _api.get_model_files(
        model_id=model_id,
        revision=revision,
        recursive=True,
        use_cookies=False if cookies is None else cookies)

    for model_file in model_files:
        if model_file['Type'] == 'tree':
            continue

        if model_file['Path'] == file_path:
            if cache.exists(model_file):
                logger.debug(
                    f'File {model_file["Name"]} already in cache, skip downloading!'
                )
                return cache.get_file_by_info(model_file)
            else:
                file_to_download_info = model_file
            break

    if file_to_download_info is None:
        raise NotExistError('The file path: %s not exist in: %s' %
                            (file_path, model_id))

    # we need to download again
    url_to_download = get_file_download_url(model_id, file_path, revision)

    if MODELSCOPE_PARALLEL_DOWNLOAD_THRESHOLD_MB * 1000 * 1000 < file_to_download_info[
            'Size'] and MODELSCOPE_DOWNLOAD_PARALLELS > 1:
        parallel_download(
            url_to_download,
            temporary_cache_dir,
            file_path,
            headers=headers,
            cookies=None if cookies is None else cookies.get_dict(),
            file_size=file_to_download_info['Size'])
    else:
        http_get_file(
            url_to_download,
            temporary_cache_dir,
            file_path,
            file_size=file_to_download_info['Size'],
            headers=headers,
            cookies=None if cookies is None else cookies.get_dict())

    temp_file_path = os.path.join(temporary_cache_dir, file_path)
    # for download with commit we can't get Sha256
    if file_to_download_info[FILE_HASH] is not None:
        file_integrity_validation(temp_file_path,
                                  file_to_download_info[FILE_HASH])
    return cache.put_file(file_to_download_info,
                          os.path.join(temporary_cache_dir, file_path))


def create_temporary_directory_and_cache(model_id: str, local_dir: str,
                                         cache_dir: str):
    group_or_owner, name = model_id_to_group_owner_name(model_id)
    if local_dir is not None:
        temporary_cache_dir = os.path.join(local_dir, TEMPORARY_FOLDER_NAME)
        cache = ModelFileSystemCache(local_dir)
    else:
        if cache_dir is None:
            cache_dir = get_model_cache_root()
        if isinstance(cache_dir, Path):
            cache_dir = str(cache_dir)
        temporary_cache_dir = os.path.join(cache_dir, TEMPORARY_FOLDER_NAME,
                                           group_or_owner, name)
        name = name.replace('.', '___')
        cache = ModelFileSystemCache(cache_dir, group_or_owner, name)

    os.makedirs(temporary_cache_dir, exist_ok=True)
    return temporary_cache_dir, cache


def get_file_download_url(model_id: str, file_path: str, revision: str):
    """Format file download url according to `model_id`, `revision` and `file_path`.
    e.g., Given `model_id=john/bert`, `revision=master`, `file_path=README.md`,
    the resulted download url is: https://modelscope.cn/api/v1/models/john/bert/repo?Revision=master&FilePath=README.md

    Args:
        model_id (str): The model_id.
        file_path (str): File path
        revision (str): File revision.

    Returns:
        str: The file url.
    """
    file_path = urllib.parse.quote_plus(file_path)
    revision = urllib.parse.quote_plus(revision)
    download_url_template = '{endpoint}/api/v1/models/{model_id}/repo?Revision={revision}&FilePath={file_path}'
    return download_url_template.format(
        endpoint=get_endpoint(),
        model_id=model_id,
        revision=revision,
        file_path=file_path,
    )


def download_part_with_retry(params):
    # unpack parameters
    model_file_path, progress, start, end, url, file_name, cookies, headers = params
    get_headers = {} if headers is None else copy.deepcopy(headers)
    get_headers['X-Request-ID'] = str(uuid.uuid4().hex)
    retry = Retry(
        total=API_FILE_DOWNLOAD_RETRY_TIMES,
        backoff_factor=1,
        allowed_methods=['GET'])
    part_file_name = model_file_path + '_%s_%s' % (start, end)
    while True:
        try:
            partial_length = 0
            if os.path.exists(
                    part_file_name):  # download partial, continue download
                with open(part_file_name, 'rb') as f:
                    partial_length = f.seek(0, io.SEEK_END)
                    progress.update(partial_length)
            start = start + partial_length
            if start > end:
                break  # this part is download completed.
            get_headers['Range'] = 'bytes=%s-%s' % (start, end)
            with open(part_file_name, 'ab+') as f:
                r = requests.get(
                    url,
                    stream=True,
                    headers=get_headers,
                    cookies=cookies,
                    timeout=API_FILE_DOWNLOAD_TIMEOUT)
                for chunk in r.iter_content(
                        chunk_size=API_FILE_DOWNLOAD_CHUNK_SIZE):
                    if chunk:  # filter out keep-alive new chunks
                        f.write(chunk)
                        progress.update(len(chunk))
            break
        except (Exception) as e:  # no matter what exception, we will retry.
            retry = retry.increment('GET', url, error=e)
            logger.warning('Downloading: %s failed, reason: %s will retry' %
                           (model_file_path, e))
            retry.sleep()


def parallel_download(
    url: str,
    local_dir: str,
    file_name: str,
    cookies: CookieJar,
    headers: Optional[Dict[str, str]] = None,
    file_size: int = None,
):
    # create temp file

    progress = tqdm(
        unit='B',
        unit_scale=True,
        unit_divisor=1024,
        total=file_size,
        initial=0,
        desc='Downloading',
    )
    PART_SIZE = 160 * 1024 * 1024  # every part is 160M
    tasks = []
    file_path = os.path.join(local_dir, file_name)
    for idx in range(int(file_size / PART_SIZE)):
        start = idx * PART_SIZE
        end = (idx + 1) * PART_SIZE - 1
        tasks.append((file_path, progress, start, end, url, file_name, cookies,
                      headers))
    if end + 1 < file_size:
        tasks.append((file_path, progress, end + 1, file_size - 1, url,
                      file_name, cookies, headers))
    parallels = MODELSCOPE_DOWNLOAD_PARALLELS if MODELSCOPE_DOWNLOAD_PARALLELS <= 4 else 4
    # download every part
    with ThreadPoolExecutor(
            max_workers=parallels, thread_name_prefix='download') as executor:
        list(executor.map(download_part_with_retry, tasks))
    progress.close()
    # merge parts.
    with open(os.path.join(local_dir, file_name), 'wb') as output_file:
        for task in tasks:
            part_file_name = task[0] + '_%s_%s' % (task[2], task[3])
            with open(part_file_name, 'rb') as part_file:
                output_file.write(part_file.read())
            os.remove(part_file_name)


def http_get_file(
    url: str,
    local_dir: str,
    file_name: str,
    file_size: int,
    cookies: CookieJar,
    headers: Optional[Dict[str, str]] = None,
):
    """Download remote file, will retry 5 times before giving up on errors.

    Args:
        url(str):
            actual download url of the file
        local_dir(str):
            local directory where the downloaded file stores
        file_name(str):
            name of the file stored in `local_dir`
        file_size(int):
            The file size.
        cookies(CookieJar):
            cookies used to authentication the user, which is used for downloading private repos
        headers(Dict[str, str], optional):
            http headers to carry necessary info when requesting the remote file

    Raises:
        FileDownloadError: File download failed.

    """
    get_headers = {} if headers is None else copy.deepcopy(headers)
    get_headers['X-Request-ID'] = str(uuid.uuid4().hex)
    temp_file_path = os.path.join(local_dir, file_name)
    logger.debug('downloading %s to %s', url, temp_file_path)
    # retry sleep 0.5s, 1s, 2s, 4s
    retry = Retry(
        total=API_FILE_DOWNLOAD_RETRY_TIMES,
        backoff_factor=1,
        allowed_methods=['GET'])
    while True:
        try:
            progress = tqdm(
                unit='B',
                unit_scale=True,
                unit_divisor=1024,
                total=file_size,
                initial=0,
                desc='Downloading',
            )
            partial_length = 0
            if os.path.exists(
                    temp_file_path):  # download partial, continue download
                with open(temp_file_path, 'rb') as f:
                    partial_length = f.seek(0, io.SEEK_END)
                    progress.update(partial_length)
            if partial_length > file_size:
                break
            get_headers['Range'] = 'bytes=%s-%s' % (partial_length,
                                                    file_size - 1)
            with open(temp_file_path, 'ab') as f:
                r = requests.get(
                    url,
                    stream=True,
                    headers=get_headers,
                    cookies=cookies,
                    timeout=API_FILE_DOWNLOAD_TIMEOUT)
                r.raise_for_status()
                for chunk in r.iter_content(
                        chunk_size=API_FILE_DOWNLOAD_CHUNK_SIZE):
                    if chunk:  # filter out keep-alive new chunks
                        progress.update(len(chunk))
                        f.write(chunk)
            progress.close()
            break
        except (Exception) as e:  # no matter what happen, we will retry.
            retry = retry.increment('GET', url, error=e)
            retry.sleep()

    logger.debug('storing %s in cache at %s', url, local_dir)
