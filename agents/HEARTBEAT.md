# HEARTBEAT.md — Agent Wake-Up Protocol

Follow this checklist every heartbeat cycle (every 15 minutes).

## Phase 1: Load Context

- [ ] Read `WORKING.md` — What was I doing?
- [ ] If task in progress, prepare to resume
- [ ] If unclear, check daily notes for recent context

## Phase 2: Check Urgent Items

- [ ] Check for undelivered @mentions / notifications
- [ ] If mentioned, handle the notification immediately
- [ ] Mark notifications as delivered after processing

## Phase 3: Check Assigned Tasks

- [ ] Query for tasks assigned to me with status `ASSIGNED`
- [ ] If found, transition to `IN_PROGRESS` and begin work
- [ ] If no assigned tasks, continue to Phase 4

## Phase 4: Scan Activity Feed

- [ ] Check recent activities for discussions relevant to my expertise
- [ ] If I have something valuable to contribute, contribute
- [ ] If nothing relevant, skip

## Phase 5: Take Action or Stand Down

- **If work found:** Do the work. Use tools. Update task status when done.
- **If nothing to do:** Report `HEARTBEAT_OK` and go back to sleep.

## Phase 6: Before Sleep

- [ ] If state changed, update `WORKING.md` with current status
- [ ] If task completed, move to `REVIEW` and note what was done
- [ ] If blocked, update task to `BLOCKED` with reason

## Rules

- One heartbeat = one unit of work. Don't try to do everything.
- If a task will take multiple heartbeats, that's fine. Update WORKING.md and continue next cycle.
- Never leave a task in ambiguous state. Always update status.
