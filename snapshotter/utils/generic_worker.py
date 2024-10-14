import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from typing import Dict
from typing import Union
from urllib.parse import urljoin

import httpx
import sha3
import tenacity
from coincurve import PrivateKey
from eip712_structs import EIP712Struct
from eip712_structs import make_domain
from eip712_structs import String
from eip712_structs import Uint
from eth_utils.encoding import big_endian_to_int
from grpclib.client import Channel
from httpx import AsyncClient
from httpx import AsyncHTTPTransport
from httpx import Client
from httpx import Limits
from httpx import Timeout
from ipfs_cid import cid_sha256_hash
from ipfs_client.dag import IPFSAsyncClientError
from ipfs_client.main import AsyncIPFSClient
from pydantic import BaseModel
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_random_exponential
from web3 import Web3

from snapshotter.settings.config import settings
from snapshotter.utils.callback_helpers import misc_notification_callback_result_handler
from snapshotter.utils.callback_helpers import send_failure_notifications_async
from snapshotter.utils.callback_helpers import send_failure_notifications_sync
from snapshotter.utils.default_logger import logger
from snapshotter.utils.file_utils import read_json_file
from snapshotter.utils.models.data_models import SnapshotterIssue
from snapshotter.utils.models.data_models import SnapshotterReportData
from snapshotter.utils.models.data_models import SnapshotterReportState
from snapshotter.utils.models.data_models import SnapshotterStatus
from snapshotter.utils.models.message_models import SnapshotProcessMessage
from snapshotter.utils.models.message_models import SnapshotSubmittedMessage
from snapshotter.utils.models.message_models import SnapshotSubmittedMessageLite
from snapshotter.utils.models.proto.snapshot_submission.submission_grpc import SubmissionStub
from snapshotter.utils.models.proto.snapshot_submission.submission_pb2 import Request
from snapshotter.utils.models.proto.snapshot_submission.submission_pb2 import SnapshotSubmission
from snapshotter.utils.rpc import RpcHelper

import grpclib


class EIPRequest(EIP712Struct):
    slotId = Uint()
    deadline = Uint()
    snapshotCid = String()
    epochId = Uint()
    projectId = String()


def web3_storage_retry_state_callback(retry_state: tenacity.RetryCallState):
    """
    Callback function to handle retry attempts for web3 storage upload.

    Args:
        retry_state (tenacity.RetryCallState): The current state of the retry call.

    Returns:
        None
    """
    if retry_state and retry_state.outcome.failed:
        logger.warning(
            f'Encountered web3 storage upload exception: {retry_state.outcome.exception()} | args: {retry_state.args}, kwargs:{retry_state.kwargs}',
        )


def relayer_submit_retry_state_callback(retry_state: tenacity.RetryCallState):
    """
    Callback function to handle retry attempts for relayer submit.

    Args:
        retry_state (tenacity.RetryCallState): The current state of the retry call.

    Returns:
        None
    """
    if retry_state and retry_state.outcome.failed:
        logger.warning(
            f'Encountered relayer submit exception: {retry_state.outcome.exception()} | args: {retry_state.args}, kwargs:{retry_state.kwargs}',
        )


def ipfs_upload_retry_state_callback(retry_state: tenacity.RetryCallState):
    """
    Callback function to handle retry attempts for IPFS uploads.

    Args:
        retry_state (tenacity.RetryCallState): The current state of the retry attempt.

    Returns:
        None
    """
    if retry_state and retry_state.outcome.failed:
        logger.warning(
            f'Encountered ipfs upload exception: {retry_state.outcome.exception()} | args: {retry_state.args}, kwargs:{retry_state.kwargs}',
        )


class GenericAsyncWorker:
    _async_transport: AsyncHTTPTransport
    _rpc_helper: RpcHelper
    _anchor_rpc_helper: RpcHelper
    _httpx_client: AsyncClient
    _web3_storage_upload_transport: AsyncHTTPTransport
    _web3_storage_upload_client: AsyncClient
    _grpc_channel: Channel
    _grpc_stub: SubmissionStub

    def __init__(self):
        """
        Initializes a GenericAsyncWorker instance.

        Args:
            name (str): The name of the worker.
            **kwargs: Additional keyword arguments to pass to the superclass constructor.
        """
        self._running_callback_tasks: Dict[str, asyncio.Task] = dict()
        self.protocol_state_contract = None

        self.protocol_state_contract_address = settings.protocol_state.address
        self.initialized = False
        self.logger = logger.bind(module='GenericAsyncWorker')
        self.status = SnapshotterStatus(projects=[])

    def _notification_callback_result_handler(self, fut: asyncio.Future):
        """
        Handles the result of a callback or notification.

        Args:
            fut (asyncio.Future): The future object representing the callback or notification.

        Returns:
            None
        """
        try:
            r = fut.result()
        except Exception as e:
            if settings.logs.trace_enabled:
                logger.opt(exception=True).error(
                    'Exception while sending callback or notification, Error: {}', e,
                )
            else:
                logger.error('Exception while sending callback or notification: {}', e)
        else:
            logger.debug('Callback or notification result:{}', r[0])

    async def _httpx_post_wrapper(self, url, req_json):
        exc = None
        try:
            r = await self._client.post(url=url, json=req_json)
        except Exception as e:
            exc = e
            r = None
        else:
            try:
                r = r.json()
            except:
                r = str(r)
        return r, exc, req_json['epochId'], req_json['projectId'], req_json['slotId']

    @retry(
        wait=wait_random_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(5),
        retry=tenacity.retry_if_not_exception_type(httpx.HTTPStatusError),
        after=web3_storage_retry_state_callback,
    )
    async def _upload_web3_storage(self, snapshot: bytes):
        """
        Uploads the given snapshot to web3 storage.

        Args:
            snapshot (bytes): The snapshot to upload.

        Returns:
            None

        Raises:
            HTTPError: If the upload fails.
        """
        web3_storage_settings = settings.web3storage
        # if no api token is provided, skip
        if not web3_storage_settings.api_token:
            return
        files = {'file': snapshot}
        r = await self._web3_storage_upload_client.post(
            url=f'{web3_storage_settings.url}{web3_storage_settings.upload_url_suffix}',
            files=files,
        )
        r.raise_for_status()
        resp = r.json()
        self.logger.info('Uploaded snapshot to web3 storage: {} | Response: {}', snapshot, resp)

    @retry(
        wait=wait_random_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(5),
        retry=tenacity.retry_if_not_exception_type(IPFSAsyncClientError),
        after=ipfs_upload_retry_state_callback,
    )
    async def _upload_to_ipfs(self, snapshot: bytes, _ipfs_writer_client: AsyncIPFSClient):
        """
        Uploads a snapshot to IPFS using the provided AsyncIPFSClient.

        Args:
            snapshot (bytes): The snapshot to upload.
            _ipfs_writer_client (AsyncIPFSClient): The IPFS client to use for uploading.

        Returns:
            str: The CID of the uploaded snapshot.
        """
        snapshot_cid = await _ipfs_writer_client.add_bytes(snapshot)
        return snapshot_cid

    
    @asynccontextmanager
    async def open_stream(self):
        try:
            async with self._grpc_stub.SubmitSnapshot.open() as stream:
                self._stream = stream
                yield self._stream
        finally:
            self._stream = None

    async def _cancel_stream(self):
        if self._stream is not None:
            try:
                await self._stream.cancel()
            except:
                self.logger.debug('Error cancelling stream, continuing...')
            self.logger.debug('Stream cancelled due to inactivity.')
            self._stream = None

    async def _send_submission_to_collector(self, snapshot_cid, epoch_id, project_id, slot_id=None, private_key=None):
        self.logger.debug(
            'Sending submission to collector...',
        )
        request_, signature, current_block_hash = await self.generate_signature(snapshot_cid, epoch_id, project_id, slot_id, private_key)

        request_msg = Request(
            slotId=request_['slotId'],
            deadline=request_['deadline'],
            snapshotCid=request_['snapshotCid'],
            epochId=request_['epochId'],
            projectId=request_['projectId'],
        )
        self.logger.debug(
            'Snapshot submission creation with request: {}', request_msg,
        )

        msg = SnapshotSubmission(request=request_msg, signature=signature.hex(), header=current_block_hash)
        self.logger.debug(
            'Snapshot submission created: {}', msg,
        )
        kwargs_simulation = {'simulation': False}
        if epoch_id == 0:
            kwargs_simulation['simulation'] = True
        
        try:
            await self._send_msg_with_closure(msg=msg, **kwargs_simulation)
        except asyncio.TimeoutError:
            self.logger.error(f'Timeout in _send_submission_to_collector while sending snapshot to local collector {msg}')
        except Exception as e:
            self.logger.error(f'Probable exception in _send_submission_to_collector while sending snapshot to local collector {msg}: {e}')
        else:
            self.logger.info('In _send_submission_to_collector successfully sent snapshot to local collector {msg}')

    async def _send_msg_with_closure(self, msg, simulation=False):
        async with self._grpc_stub.SubmitSnapshot.open() as stream:
            try:
                await stream.send_message(msg)
                self.logger.debug(f'Sent message to local collector: {msg}')
                response = await stream.recv_message()
                self.logger.debug(f'Received response from local collector for {msg}: {response}')
                if simulation:
                    self.logger.info('✅ Simulation snapshot submitted successfully to local collector: {}!', msg)
                else:
                    self.logger.info('✅ Snapshot submitted successfully to local collector: {}!', msg)
                return
                await stream.end()
            except (ConnectionResetError, grpclib.exceptions.GRPCError) as e:
                self.logger.error(f'Probable grpc failure to send snapshot {msg}, got error: {e}. Will try to cancel and re-init the stream')
                try:
                    await asyncio.wait_for(stream.cancel(), timeout=10)
                except asyncio.TimeoutError:
                    self.logger.error(f'Timeout in cancelling stream for snapshot {msg}')
                except asyncio.CancelledError:
                    self.logger.info(f'Stream for snapshot {msg} was already cancelled')
                except Exception as e:
                    self.logger.error(f'Probable exception in cancelling stream for snapshot {msg}: {e}')
            except asyncio.CancelledError:
                self.logger.info('Task to send snapshot to local collector was asyncio cancelled! {}', msg)
            except Exception as e:
                self.logger.error(f'Probable failure to send snapshot {msg}, got error: {e}')
            else:
                self.logger.debug(f'Finalized snapshot submission to local collector without errors: {msg}')
    

    async def _commit_payload(
            self,
            task_type: str,
            _ipfs_writer_client: AsyncIPFSClient,
            project_id: str,
            epoch: Union[
                SnapshotProcessMessage,
                SnapshotSubmittedMessage
            ],
            snapshot: BaseModel,
            storage_flag: bool,
    ):
        """
        Commits the given snapshot to IPFS and web3 storage (if enabled), and sends messages to the event detector and relayer
        dispatch queues.

        Args:
            task_type (str): The type of task being committed.
            _ipfs_writer_client (AsyncIPFSClient): The IPFS client to use for uploading the snapshot.
            project_id (str): The ID of the project the snapshot belongs to.
            epoch (Union[SnapshotProcessMessage, SnapshotSubmittedMessage,
            SnapshotSubmittedMessageLite]): The epoch the snapshot belongs to.
            snapshot (BaseModel): The snapshot to commit.
            storage_flag (bool): Whether to upload the snapshot to web3 storage.

        Returns:
            snapshot_cid (str): The CID of the uploaded snapshot.
        """
        # upload to IPFS
        snapshot_json = json.dumps(snapshot.dict(by_alias=True), sort_keys=True, separators=(',', ':'))
        snapshot_bytes = snapshot_json.encode('utf-8')
        try:
            if settings.ipfs.url:
                snapshot_cid = await self._upload_to_ipfs(snapshot_bytes, _ipfs_writer_client)
            else:
                snapshot_cid = cid_sha256_hash(snapshot_bytes)
        except Exception as e:
            self.logger.opt(exception=True).error(
                'Exception uploading snapshot to IPFS for epoch {}: {}, Error: {},'
                'sending failure notifications', epoch, snapshot, e,
            )
            self.status.totalMissedSubmissions += 1
            self.status.consecutiveMissedSubmissions += 1
            notification_message = SnapshotterReportData(
                snapshotterIssue=SnapshotterIssue(
                    instanceID=settings.instance_id,
                    issueType=SnapshotterReportState.MISSED_SNAPSHOT.value,
                    projectID=project_id,
                    epochId=str(epoch.epochId),
                    timeOfReporting=str(time.time()),
                    extra=json.dumps({'issueDetails': f'Error : {e}'}),
                ),
                snapshotterStatus=self.status,
            )
            await send_failure_notifications_async(
                client=self._client, message=notification_message,
            )
        else:
            # submit to collector
            try:
                await self._send_submission_to_collector(snapshot_cid, epoch.epochId, project_id)
            except Exception as e:
                self.logger.opt(exception=True).error(
                    'Exception submitting snapshot to collector for epoch {}: {}, Error: {},'
                    'sending failure notifications', epoch, snapshot, e,
                )
                self.status.totalMissedSubmissions += 1
                self.status.consecutiveMissedSubmissions += 1

                notification_message = SnapshotterReportData(
                    snapshotterIssue=SnapshotterIssue(
                        instanceID=settings.instance_id,
                        issueType=SnapshotterReportState.MISSED_SNAPSHOT.value,
                        projectID=project_id,
                        epochId=str(epoch.epochId),
                        timeOfReporting=str(time.time()),
                        extra=json.dumps({'issueDetails': f'Error : {e}'}),
                    ),
                    snapshotterStatus=self.status,
                )
                await send_failure_notifications_async(
                    client=self._client, message=notification_message,
                )
            else:
                # reset consecutive missed snapshots counter
                self.status.consecutiveMissedSubmissions = 0
                self.status.totalSuccessfulSubmissions += 1

        # upload to web3 storage
        if storage_flag:
            asyncio.ensure_future(self._upload_web3_storage(snapshot_bytes))

        return snapshot_cid

    async def _init_rpc_helper(self):
        """
        Initializes the RpcHelper objects for the worker and anchor chain, and sets up the protocol state contract.
        """
        self._rpc_helper = RpcHelper(rpc_settings=settings.rpc)
        self._anchor_rpc_helper = RpcHelper(rpc_settings=settings.anchor_chain_rpc)

        self.protocol_state_contract = self._anchor_rpc_helper.get_current_node()['web3_client'].eth.contract(
            address=Web3.to_checksum_address(
                self.protocol_state_contract_address,
            ),
            abi=read_json_file(
                settings.protocol_state.abi,
                self.logger,
            ),
        )

        self._anchor_chain_id = self._anchor_rpc_helper.get_current_node()['web3_client'].eth.chain_id
        self._keccak_hash = lambda x: sha3.keccak_256(x).digest()
        self._domain_separator = make_domain(
            name='PowerloomProtocolContract', version='0.1', chainId=self._anchor_chain_id,
            verifyingContract=self.protocol_state_contract_address,
        )
        self._private_key = settings.signer_private_key
        if self._private_key.startswith('0x'):
            self._private_key = self._private_key[2:]
        self._identity_private_key = PrivateKey.from_hex(settings.signer_private_key)

    async def generate_signature(self, snapshot_cid, epoch_id, project_id, slot_id=None, private_key=None):
        current_block = await self._anchor_rpc_helper.eth_get_block()
        current_block_number = int(current_block['number'], 16)
        current_block_hash = current_block['hash']
        deadline = current_block_number + settings.protocol_state.deadline_buffer
        request_slot_id = settings.slot_id if not slot_id else slot_id
        request = EIPRequest(
            slotId=request_slot_id,
            deadline=deadline,
            snapshotCid=snapshot_cid,
            epochId=epoch_id,
            projectId=project_id,
        )

        signable_bytes = request.signable_bytes(self._domain_separator)
        if not private_key:
            signature = self._identity_private_key.sign_recoverable(signable_bytes, hasher=self._keccak_hash)
        else:
            if private_key.startswith('0x'):
                private_key = private_key[2:]
            signer_private_key = PrivateKey.from_hex(private_key)
            signature = signer_private_key.sign_recoverable(signable_bytes, hasher=self._keccak_hash)
        v = signature[64] + 27
        r = big_endian_to_int(signature[0:32])
        s = big_endian_to_int(signature[32:64])

        final_sig = r.to_bytes(32, 'big') + s.to_bytes(32, 'big') + v.to_bytes(1, 'big')
        request_ = {'slotId': request_slot_id, 'deadline': deadline, 'snapshotCid': snapshot_cid, 'epochId': epoch_id, 'projectId': project_id}
        return request_, final_sig, current_block_hash

    async def _init_httpx_client(self):
        """
        Initializes the HTTPX client and transport objects for making HTTP requests.
        """
        self._async_transport = AsyncHTTPTransport(
            limits=Limits(
                max_connections=200,
                max_keepalive_connections=50,
                keepalive_expiry=None,
            ),
        )
        self._client = AsyncClient(
            timeout=Timeout(timeout=5.0),
            follow_redirects=False,
            transport=self._async_transport,
        )
        self._web3_storage_upload_transport = AsyncHTTPTransport(
            limits=Limits(
                max_connections=200,
                max_keepalive_connections=settings.web3storage.max_idle_conns,
                keepalive_expiry=settings.web3storage.idle_conn_timeout,
            ),
        )
        self._web3_storage_upload_client = AsyncClient(
            timeout=Timeout(timeout=settings.web3storage.timeout),
            follow_redirects=False,
            transport=self._web3_storage_upload_transport,
            headers={'Authorization': 'Bearer ' + settings.web3storage.api_token},
        )

    async def _init_grpc(self):
        self._grpc_channel = Channel(
            host='host.docker.internal',
            port=settings.local_collector_port,
            ssl=False,
        )
        self._grpc_stub = SubmissionStub(self._grpc_channel)
        self._stream = None
        self._cancel_task = None

    async def _init_protocol_meta(self):
        # TODO: combine these into a single call
        try:
            source_block_time = await self._anchor_rpc_helper.web3_call(
                [
                    self.protocol_state_contract.functions.SOURCE_CHAIN_BLOCK_TIME(
                    Web3.to_checksum_address(settings.data_market),
                    ),
                ],
            )
        except Exception as e:
            self.logger.exception(
                'Exception in querying protocol state for source chain block time: {}',
                e,
            )
        else:
            source_block_time = source_block_time[0]
            self._source_chain_block_time = source_block_time / 10 ** 4
            self.logger.debug('Set source chain block time to {}', self._source_chain_block_time)
        try:
            epoch_size = await self._anchor_rpc_helper.web3_call(
                [self.protocol_state_contract.functions.EPOCH_SIZE(Web3.to_checksum_address(settings.data_market))],
            )
        except Exception as e:
            self.logger.exception(
                'Exception in querying protocol state for epoch size: {}',
                e,
            )
        else:
            self._epoch_size = epoch_size[0]
            self.logger.debug('Set epoch size to {}', self._epoch_size)

    async def init(self):
        """
        Initializes the worker by initializing the HTTPX client, and RPC helper.
        """
        if not self.initialized:
            await self._init_httpx_client()
            await self._init_rpc_helper()
            await self._init_protocol_meta()
            await self._init_grpc()
        self.initialized = True
