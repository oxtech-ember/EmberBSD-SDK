/* SPDX-License-Identifier: BSD-2-Clause */
/* Native header qualification. Compile only with host GCC16 on NetBSD 11. */
#define _NETBSD_SOURCE 1
#include <sys/types.h>
#include <sys/sysctl.h>
#include <sys/event.h>
#include <stddef.h>
#include <stdio.h>
_Static_assert(offsetof(struct kinfo_proc2, p_pid) == 116, "p_pid");
_Static_assert(offsetof(struct kinfo_proc2, p_uid) == 136, "p_uid");
_Static_assert(offsetof(struct kinfo_proc2, p_stat) == 360, "p_stat");
_Static_assert(offsetof(struct kinfo_proc2, p_acflag) + 2 == 368, "prefix");
_Static_assert(sizeof(struct kinfo_proc2) >= 368, "record");
_Static_assert(KERN_PROC2 == 47 && KERN_PROC_ALL == 0, "MIB");
_Static_assert(NOTE_TRACK == 1 && NOTE_TRACKERR == 2 && NOTE_CHILD == 4, "track");
int main(void)
{
    printf("NetBSD process prefix: pid=%zu uid=%zu state=%zu stride=368 full=%zu\n",
        offsetof(struct kinfo_proc2, p_pid), offsetof(struct kinfo_proc2, p_uid),
        offsetof(struct kinfo_proc2, p_stat), sizeof(struct kinfo_proc2));
    return 0;
}
