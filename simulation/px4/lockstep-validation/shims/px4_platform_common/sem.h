#pragma once
#include <semaphore.h>
using px4_sem_t=sem_t;
#define px4_sem_init sem_init
#define px4_sem_destroy sem_destroy
#define px4_sem_wait sem_wait
#define px4_sem_trywait sem_trywait
#define px4_sem_post sem_post
#define px4_sem_getvalue sem_getvalue
