/* Test-only subscription policy for an isolated loopback Mosquitto process. */
#include <string.h>
#include <mosquitto.h>
#include <mosquitto_broker.h>
#include <mosquitto_plugin.h>

static mosquitto_plugin_id_t *plugin_id;
static const char *denied;

static int acl(int event, void *data, void *userdata)
{
    struct mosquitto_evt_acl_check *check = data;
    (void)event;
    (void)userdata;
    if(check->access == MOSQ_ACL_SUBSCRIBE && strcmp(check->topic, denied) == 0){
        return MOSQ_ERR_ACL_DENIED;
    }
    return MOSQ_ERR_SUCCESS;
}

int mosquitto_plugin_version(int count, const int *versions)
{
    for(int i = 0; i < count; i++){
        if(versions[i] == 5) return 5;
    }
    return -1;
}

int mosquitto_plugin_init(mosquitto_plugin_id_t *id, void **data,
                         struct mosquitto_opt *opts, int count)
{
    (void)data;
    for(int i = 0; i < count; i++){
        if(strcmp(opts[i].key, "deny") == 0) denied = opts[i].value;
    }
    if(!denied) return MOSQ_ERR_INVAL;
    plugin_id = id;
    return mosquitto_callback_register(id, MOSQ_EVT_ACL_CHECK, acl, NULL, NULL);
}

int mosquitto_plugin_cleanup(void *data, struct mosquitto_opt *opts, int count)
{
    (void)data;
    (void)opts;
    (void)count;
    return mosquitto_callback_unregister(plugin_id, MOSQ_EVT_ACL_CHECK, acl, NULL);
}
