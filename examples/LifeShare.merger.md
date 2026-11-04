LifeShare merge rounds (Unreal 5.8, on Alex's PC). Alex has standing approval for every merge: never ask him.

PC rules (Docs/AGENTS.md and the PC work notes):
- Never touch E:\UnrealProjects\TrespassFishin, and never pull in E:\UnrealProjects\LifeShare.
- Never run `git worktree prune`. Only stop processes that name your own worktree (never Stop-Process -Name, taskkill /IM
  or pkill). Don't use `git clean` or `git stash -u` in PowerShell (the lifeshare-guard hook denies them); to drop a
  merge, use `git merge --abort` or `git reset --hard`.
- Run builds and imports at BelowNormal priority. The Tools scripts take the PC's heavy lease themselves
  (Tools\CI\LaneGuard.ps1). One that prints "RESULT: NO RESULT" and exits 2 found the PC busy with a build job: that
  is not a failure; wait 10 minutes and run it again. Never pass -AllowLaneRunning: a job would kill the run.
- Don't push to Test-Staging while a checkpoint or release build of it is running; land after it finishes.
- `git lfs pull` in the worktree before building; push with `Tools\CI\PushWithLfs.ps1` when a merge adds LFS files.

Order of a round (the last round that landed, #916, ran exactly this):
1. Merge the PRs into the round branch (merge commits; broad renames first). LSStrings.csv conflicts are additive: keep
   both sides. Save version constants: take the highest and bump once. DefaultEngine.ini CoreRedirects: rerun the
   generator script.
2. `Tools\Build.ps1` until `BUILD OK`. Fix compile errors the combination causes here (shadowed locals, unity-build name
   clashes, duplicate blocks).
3. Content and imports the PRs ask for (their `Lane-Imports:` line and build notes), always `Tools\BuildContent.ps1`
   FIRST, before any NavCheck or Game specs. Then, as needed: ImportFurniture, CheckHomes, BuildTown / BuildMaps,
   UpdateWillowStreet, icon and audio imports, character stages. A pin PR (Folk round / wardrobe) installs its delivery
   first, as its README says.
4. Checks: `Tools\RunNavCheck.ps1 -StrictSlots` for any PR that touches furniture, homes, venues, the town or slots
   (Content/Town, Content/HomeKit, Tools/HomeKit, Furniture/Venue/Slot sources: the Merge section flags these as
   "strict NavCheck on the PC"). Then the Game specs (`Tools\RunAllTests.ps1 -Set Game`) and each PR's own named
   specs and console checks. Docs/QA.md lists them.
5. On a failure, find the PR that caused it (leave PRs out to bisect), fix it if small, else leave it out and comment on
   it with the failing step and error lines.
6. Land: one "Merge round N" PR into Test-Staging, merged with a merge commit. Fletchery closes the tasks.

Keep the round's progress file updated with these step names: "Merge the PRs into the round branch", "Build",
"BuildContent and imports", "Strict NavCheck", "Game specs", "Land on Test-Staging".
