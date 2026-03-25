import asyncio
import json
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, WebSocket, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from api.auth import get_current_user, get_ws_user
from api.database import get_db
from api.db_models import Job
from api.rate_limit import limiter
from api.schemas import JobStatus, JobStatusResponse, RunResponse
from code_gen.agent import CodeGenAgent
from code_gen.tools import ObservationResult

router = APIRouter(prefix="/agent", tags=["agent"])

UPLOAD_DIR = Path("./uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# Track active WebSocket connections per job for ask_human interaction
_ws_connections: dict[str, WebSocket] = {}
_human_futures: dict[str, deque[asyncio.Future]] = {}
_agent_tasks: dict[str, asyncio.Task] = {}


@router.post("/run", response_model=RunResponse)
@limiter.limit("5/minute")
async def run_agent(
	request: Request,
	file: UploadFile,
	db: Session = Depends(get_db),
	username: str = Depends(get_current_user),
):
	if not file.filename or not file.filename.endswith(".xlsx"):
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Only .xlsx files are accepted",
		)

	job_id = str(uuid.uuid4())

	# Save uploaded file
	job_upload_dir = UPLOAD_DIR / job_id
	job_upload_dir.mkdir(parents=True, exist_ok=True)
	file_path = job_upload_dir / file.filename
	content = await file.read()
	file_path.write_bytes(content)

	# Create job record in DB
	job = Job(
		job_id=job_id,
		username=username,
		status=JobStatus.PENDING.value,
		created_at=datetime.now(UTC),
	)
	db.add(job)
	db.commit()

	# Launch agent as background task
	task = asyncio.create_task(_run_agent(job_id, str(file_path)))
	_agent_tasks[job_id] = task

	return RunResponse(job_id=job_id, status=JobStatus.PENDING)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(
	job_id: str,
	db: Session = Depends(get_db),
	username: str = Depends(get_current_user),
):
	job = db.query(Job).filter(Job.job_id == job_id).first()
	if not job:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
	if job.username != username:
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your job")
	return JobStatusResponse(
		job_id=job.job_id,
		status=JobStatus(job.status),
		created_at=job.created_at,
		result=job.result,
		error=job.error,
		output_files=job.output_files or [],
	)


@router.get("/jobs/{job_id}/output")
def download_output(
	job_id: str,
	db: Session = Depends(get_db),
	username: str = Depends(get_current_user),
):
	job = db.query(Job).filter(Job.job_id == job_id).first()
	if not job:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
	if job.username != username:
		raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your job")
	if job.status != JobStatus.COMPLETED.value:
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job not completed")

	output_files = job.output_files or []
	if not output_files:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No output files")

	# Return the first output file
	file_path = Path(output_files[0])
	if not file_path.exists():
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output file not found")

	return FileResponse(
		path=str(file_path),
		filename=file_path.name,
		media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
	)


@router.websocket("/ws/{job_id}")
async def websocket_endpoint(websocket: WebSocket, job_id: str, token: str = ""):
	# Authenticate via query param token
	try:
		username = get_ws_user(token)
	except HTTPException:
		await websocket.close(code=4001, reason="Unauthorized")
		return

	# Verify job ownership
	db = next(get_db())
	try:
		job = db.query(Job).filter(Job.job_id == job_id).first()
		if not job or job.username != username:
			await websocket.close(code=4003, reason="Forbidden")
			return
	finally:
		db.close()

	await websocket.accept()
	_ws_connections[job_id] = websocket

	# Send current job status immediately so the client catches up
	db2 = next(get_db())
	try:
		current_job = db2.query(Job).filter(Job.job_id == job_id).first()
		if current_job:
			if current_job.status == JobStatus.FAILED.value:
				await websocket.send_text(
					json.dumps({"type": "failed", "error": current_job.error or "Unknown error"})
				)
			elif current_job.status == JobStatus.COMPLETED.value:
				await websocket.send_text(
					json.dumps(
						{"type": "completed", "output_files": current_job.output_files or []}
					)
				)
			elif current_job.status == JobStatus.RUNNING.value:
				await websocket.send_text(
					json.dumps({"type": "status", "status": "running", "step": 0, "tool": None})
				)
	finally:
		db2.close()

	try:
		# Listen for messages from client (human responses + kill)
		while True:
			data = await websocket.receive_text()
			msg = json.loads(data)
			if msg.get("type") == "human_response" and _human_futures.get(job_id):
				future = _human_futures[job_id].popleft()
				if not future.done():
					future.set_result(msg.get("answer", ""))
			elif msg.get("type") == "kill":
				await _kill_agent(job_id)
	except WebSocketDisconnect:
		pass
	finally:
		_ws_connections.pop(job_id, None)
		# Resolve any pending futures so the agent doesn't hang
		futures = _human_futures.pop(job_id, deque())
		for future in futures:
			if not future.done():
				future.set_result("No response — user disconnected.")


async def _ws_send(job_id: str, data: dict):
	"""Send a JSON message to the WebSocket client for a given job."""
	ws = _ws_connections.get(job_id)
	if ws:
		try:
			await ws.send_text(json.dumps(data))
		except Exception:
			pass


async def _kill_agent(job_id: str):
	"""Cancel the background agent task and mark the job as failed."""
	task = _agent_tasks.pop(job_id, None)
	if task and not task.done():
		task.cancel()

	# Resolve any pending ask_human futures
	futures = _human_futures.pop(job_id, deque())
	for future in futures:
		if not future.done():
			future.set_result("Agent killed by user.")

	# Update DB
	from api.database import SessionLocal

	db = SessionLocal()
	try:
		job = db.query(Job).filter(Job.job_id == job_id).first()
		if job and job.status not in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
			job.status = JobStatus.FAILED.value
			job.error = "Killed by user"
			db.commit()
	finally:
		db.close()

	await _ws_send(job_id, {"type": "failed", "error": "Killed by user"})


def _make_ask_human(job_id: str):
	"""Create an ask_human tool override that uses WebSocket for interaction."""

	async def ws_ask_human(question: str) -> ObservationResult:
		await _ws_send(job_id, {"type": "ask_human", "question": question})

		# Create a future and enqueue it
		loop = asyncio.get_event_loop()
		future = loop.create_future()
		_human_futures.setdefault(job_id, deque()).append(future)

		try:
			answer = await asyncio.wait_for(future, timeout=300)  # 5 min timeout
		except TimeoutError:
			answer = "No response from user (timed out). Proceed with your best judgment."

		return ObservationResult(tool="ask_human", success=True, output=answer)

	return ws_ask_human


async def _run_agent(job_id: str, file_path: str):
	"""Background coroutine that runs the CodeGenAgent and updates the DB."""
	from api.database import SessionLocal

	db = SessionLocal()

	try:
		job = db.query(Job).filter(Job.job_id == job_id).first()
		job.status = JobStatus.RUNNING.value
		db.commit()

		await _ws_send(job_id, {"type": "status", "status": "running", "step": 0, "tool": None})

		async def on_tool_start(step: int, tool_name: str):
			await _ws_send(
				job_id,
				{
					"type": "tool_start",
					"status": "running",
					"step": step,
					"tool": tool_name,
				},
			)

		async def on_step(step: int, tool_name: str, observation: ObservationResult):
			await _ws_send(
				job_id,
				{
					"type": "tool_done",
					"status": "running",
					"step": step,
					"tool": tool_name,
					"success": observation.success,
				},
			)

		agent = CodeGenAgent(
			ask_human_fn=_make_ask_human(job_id),
			on_tool_start=on_tool_start,
			on_step=on_step,
		)
		result = await agent.run(input_file_path=file_path)

		if result.get("terminated"):
			job.status = JobStatus.FAILED.value
			job.error = result.get("terminate_reason", "Agent terminated")
			db.commit()

			await _ws_send(
				job_id,
				{
					"type": "terminated",
					"reason": job.error,
				},
			)
		else:
			job.status = JobStatus.COMPLETED.value
			job.result = result.get("messages")
			job.output_files = result.get("output_files", [])
			db.commit()

			await _ws_send(
				job_id,
				{
					"type": "completed",
					"output_files": job.output_files,
				},
			)

	except asyncio.CancelledError:
		# Agent was killed — DB already updated by _kill_agent
		pass

	except Exception as e:
		job.status = JobStatus.FAILED.value
		job.error = str(e)
		db.commit()

		await _ws_send(job_id, {"type": "failed", "error": str(e)})

	finally:
		_agent_tasks.pop(job_id, None)
		db.close()
