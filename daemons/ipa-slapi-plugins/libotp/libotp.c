/** BEGIN COPYRIGHT BLOCK
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 *
 * Additional permission under GPLv3 section 7:
 *
 * In the following paragraph, "GPL" means the GNU General Public
 * License, version 3 or any later version, and "Non-GPL Code" means
 * code that is governed neither by the GPL nor a license
 * compatible with the GPL.
 *
 * You may link the code of this Program with Non-GPL Code and convey
 * linked combinations including the two, provided that such Non-GPL
 * Code only links to the code of this Program through those well
 * defined interfaces identified in the file named EXCEPTION found in
 * the source code files (the "Approved Interfaces"). The files of
 * Non-GPL Code may instantiate templates or use macros or inline
 * functions from the Approved Interfaces without causing the resulting
 * work to be covered by the GPL. Only the copyright holders of this
 * Program may make changes or additions to the list of Approved
 * Interfaces.
 *
 * Authors:
 * Nathaniel McCallum <npmccallum@redhat.com>
 *
 * Copyright (C) 2013 Red Hat, Inc.
 * All rights reserved.
 * END COPYRIGHT BLOCK **/

#include "libotp.h"
#include "librfc.h"

#include <time.h>
#include <errno.h>

#define TOKEN(s) "ipaToken" s
#define O(s) TOKEN("OTP" s)
#define T(s) TOKEN("TOTP" s)
#define H(s) TOKEN("HOTP" s)

#define IPA_OTP_DEFAULT_TOKEN_STEP 30
#define IPA_OTP_OBJCLS_FILTER \
    "(|(objectClass=ipaTokenTOTP)(objectClass=ipaTokenHOTP))"


enum otptoken_type {
    OTPTOKEN_NONE = 0,
    OTPTOKEN_TOTP,
    OTPTOKEN_HOTP,
};

struct otptoken {
    Slapi_ComponentId *plugin_id;
    Slapi_DN *sdn;
    struct hotp_token token;
    enum otptoken_type type;
    union {
        struct {
            uint64_t watermark;
            unsigned int step;
            int offset;
        } totp;
        struct {
            uint64_t counter;
        } hotp;
    };
};

static const char *get_basedn(Slapi_DN *dn)
{
    Slapi_DN *suffix = NULL;
    void *node = NULL;

    for (suffix = slapi_get_first_suffix(&node, 0);
         suffix != NULL;
         suffix = slapi_get_next_suffix(&node, 0)) {
        if (slapi_sdn_issuffix(dn, suffix))
            return (char *) slapi_sdn_get_dn(suffix);
    }

    return NULL;
}

static inline bool is_algo_valid(const char *algo)
{
    static const char *valid_algos[] = { "sha1", "sha256", "sha384",
                                         "sha512", NULL };
    int i, ret;

    for (i = 0; valid_algos[i]; i++) {
        ret = strcasecmp(algo, valid_algos[i]);
        if (ret == 0)
            return true;
    }

    return false;
}

static const struct berval *entry_attr_get_berval(const Slapi_Entry* e,
                                                  const char *type)
{
    Slapi_Attr* attr = NULL;
    Slapi_Value *v;
    int ret;

    ret = slapi_entry_attr_find(e, type, &attr);
    if (ret != 0 || attr == NULL)
        return NULL;

    ret = slapi_attr_first_value(attr, &v);
    if (ret < 0)
        return NULL;

    return slapi_value_get_berval(v);
}

static bool writeattr(const struct otptoken *token, const char *attr,
                      int value)
{
    Slapi_Value *svals[] = { NULL, NULL };
    Slapi_PBlock *pb = NULL;
    Slapi_Mods *mods = NULL;
    bool success = false;
    int ret;

    /* Create the value. */
    svals[0] = slapi_value_new();
    if (slapi_value_set_int(svals[0], value) != 0) {
        slapi_value_free(&svals[0]);
        return false;
    }

    /* Create the mods. */
    mods = slapi_mods_new();
    slapi_mods_add_mod_values(mods, LDAP_MOD_REPLACE, attr, svals);

    /* Perform the modification. */
    pb = slapi_pblock_new();
    slapi_modify_internal_set_pb(pb, slapi_sdn_get_dn(token->sdn),
                                 slapi_mods_get_ldapmods_byref(mods),
                                 NULL, NULL, token->plugin_id, 0);
    if (slapi_modify_internal_pb(pb) != 0)
        goto error;
    if (slapi_pblock_get(pb, SLAPI_PLUGIN_INTOP_RESULT, &ret) != 0)
        goto error;
    if (ret != LDAP_SUCCESS)
        goto error;

    success = true;

error:
    slapi_pblock_destroy(pb);
    slapi_mods_free(&mods);
    return success;

}

/**
 * Validate a token.
 *
 * If the second token code is specified, perform synchronization.
 */
static bool validate(struct otptoken *token, time_t now, ssize_t step,
                     uint32_t first, const uint32_t *second)
{
    const char *attr;
    uint32_t tmp;

    /* Calculate the absolute step. */
    switch (token->type) {
    case OTPTOKEN_TOTP:
        attr = T("watermark");
        step = (now + token->totp.offset) / token->totp.step + step;
        if (token->totp.watermark > 0 && step < token->totp.watermark)
            return false;
        break;
    case OTPTOKEN_HOTP:
        if (step < 0) /* NEVER go backwards! */
            return false;
        attr = H("counter");
        step = token->hotp.counter + step;
        break;
    default:
        return false;
    }

    /* Validate the first code. */
    if (!hotp(&token->token, step++, &tmp))
        return false;

    if (first != tmp)
        return false;

    /* Validate the second code if specified. */
    if (second != NULL) {
        if (!hotp(&token->token, step++, &tmp))
            return false;

        if (*second != tmp)
            return false;

        /* Perform optional synchronization steps. */
        switch (token->type) {
        case OTPTOKEN_TOTP:
            tmp = (step - now / token->totp.step) * token->totp.step;
            if (!writeattr(token, T("clockOffset"), tmp))
                return false;
            break;
        default:
            break;
        }
    }

    /* Write the step value. */
    if (!writeattr(token, attr, step))
        return false;

    /* Save our modifications to the object. */
    switch (token->type) {
    case OTPTOKEN_TOTP:
        token->totp.watermark = step;
        break;
    case OTPTOKEN_HOTP:
        token->hotp.counter = step;
        break;
    default:
        break;
    }

    return true;
}


static void otptoken_free(struct otptoken *token)
{
    if (token == NULL)
        return;

    slapi_sdn_free(&token->sdn);
    free(token->token.key.bytes);
    slapi_ch_free_string(&token->token.algo);
    free(token);
}

void otptoken_free_array(struct otptoken **tokens)
{
    if (tokens == NULL)
        return;

    for (size_t i = 0; tokens[i] != NULL; i++)
        otptoken_free(tokens[i]);

    free(tokens);
}

static struct otptoken *otptoken_new(Slapi_ComponentId *id, Slapi_Entry *entry)
{
    const struct berval *tmp;
    struct otptoken *token;
    char **vals;

    token = calloc(1, sizeof(struct otptoken));
    if (token == NULL)
        return NULL;
    token->plugin_id = id;

    /* Get the token type. */
    vals = slapi_entry_attr_get_charray(entry, "objectClass");
    if (vals == NULL)
        goto error;
    token->type = OTPTOKEN_NONE;
    for (int i = 0; vals[i] != NULL; i++) {
        if (strcasecmp(vals[i], "ipaTokenTOTP") == 0)
            token->type = OTPTOKEN_TOTP;
        else if (strcasecmp(vals[i], "ipaTokenHOTP") == 0)
            token->type = OTPTOKEN_HOTP;
    }
    slapi_ch_array_free(vals);
    if (token->type == OTPTOKEN_NONE)
        goto error;

    /* Get SDN. */
    token->sdn = slapi_sdn_dup(slapi_entry_get_sdn(entry));
    if (token->sdn == NULL)
        goto error;

    /* Get key. */
    tmp = entry_attr_get_berval(entry, O("key"));
    if (tmp == NULL)
        goto error;
    token->token.key.len = tmp->bv_len;
    token->token.key.bytes = malloc(token->token.key.len);
    if (token->token.key.bytes == NULL)
        goto error;
    memcpy(token->token.key.bytes, tmp->bv_val, token->token.key.len);

    /* Get length. */
    token->token.digits = slapi_entry_attr_get_int(entry, O("digits"));
    if (token->token.digits != 6 && token->token.digits != 8)
        goto error;

    /* Get algorithm. */
    token->token.algo = slapi_entry_attr_get_charptr(entry, O("algorithm"));
    if (token->token.algo == NULL)
        token->token.algo = slapi_ch_strdup("sha1");
    if (!is_algo_valid(token->token.algo))
        goto error;

    switch (token->type) {
    case OTPTOKEN_TOTP:
        /* Get offset. */
        token->totp.offset = slapi_entry_attr_get_int(entry, T("clockOffset"));

        /* Get watermark. */
        token->totp.watermark = slapi_entry_attr_get_int(entry, T("watermark"));

        /* Get step. */
        token->totp.step = slapi_entry_attr_get_uint(entry, T("timeStep"));
        if (token->totp.step == 0)
            token->totp.step = IPA_OTP_DEFAULT_TOKEN_STEP;
        break;
    case OTPTOKEN_HOTP:
        /* Get counter. */
        token->hotp.counter = slapi_entry_attr_get_int(entry, H("counter"));
        break;
    default:
        break;
    }

    return token;

error:
    otptoken_free(token);
    return NULL;
}

static struct otptoken **find(Slapi_ComponentId *id, const char *user_dn,
                              const char *token_dn, const char *intfilter,
                              const char *extfilter)
{
    struct otptoken **tokens = NULL;
    Slapi_Entry **entries = NULL;
    Slapi_PBlock *pb = NULL;
    Slapi_DN *sdn = NULL;
    char *filter = NULL;
    const char *basedn = NULL;
    size_t count = 0;
    int result = -1;

    if (intfilter == NULL)
        intfilter = "";

    if (extfilter == NULL)
        extfilter = "";

    /* Create the filter. */
    if (user_dn == NULL) {
        filter = "(&" IPA_OTP_OBJCLS_FILTER "%s%s)";
        filter = slapi_filter_sprintf(filter, intfilter, extfilter);
    } else {
        filter = "(&" IPA_OTP_OBJCLS_FILTER "(ipatokenOwner=%s%s)%s%s)";
        filter = slapi_filter_sprintf(filter, ESC_AND_NORM_NEXT_VAL,
                                      user_dn, intfilter, extfilter);
    }

    /* Create the search. */
    pb = slapi_pblock_new();
    if (token_dn != NULL) {
        /* Find only the token specified. */
        slapi_search_internal_set_pb(pb, token_dn, LDAP_SCOPE_BASE, filter,
                                     NULL, 0, NULL, NULL, id, 0);
    } else {
        sdn = slapi_sdn_new_dn_byval(user_dn);
        if (sdn == NULL)
            goto error;

        basedn = get_basedn(sdn);
        if (basedn == NULL)
            goto error;

        /* Find all user tokens. */
        slapi_search_internal_set_pb(pb, basedn,
                                     LDAP_SCOPE_SUBTREE, filter, NULL,
                                     0, NULL, NULL, id, 0);
    }
    slapi_search_internal_pb(pb);
    slapi_ch_free_string(&filter);

    /* Get the results. */
    slapi_pblock_get(pb, SLAPI_PLUGIN_INTOP_RESULT, &result);
    if (result != LDAP_SUCCESS)
        goto error;
    slapi_pblock_get(pb, SLAPI_PLUGIN_INTOP_SEARCH_ENTRIES, &entries);
    if (entries == NULL)
        goto error;

    /* TODO: Can I get the count another way? */
    for (count = 0; entries[count] != NULL; count++)
        continue;

    /* Create the array. */
    tokens = calloc(count + 1, sizeof(*tokens));
    if (tokens == NULL)
        goto error;
    for (count = 0; entries[count] != NULL; count++) {
        tokens[count] = otptoken_new(id, entries[count]);
        if (tokens[count] == NULL) {
            otptoken_free_array(tokens);
            tokens = NULL;
            goto error;
        }
    }

error:
    if (sdn != NULL)
        slapi_sdn_free(&sdn);
    slapi_pblock_destroy(pb);
    return tokens;
}

struct otptoken **otptoken_find(Slapi_ComponentId *id, const char *user_dn,
                                const char *token_dn, bool active,
                                const char *filter)
{
    static const char template[] =
    "(|(ipatokenNotBefore<=%04d%02d%02d%02d%02d%02dZ)(!(ipatokenNotBefore=*)))"
    "(|(ipatokenNotAfter>=%04d%02d%02d%02d%02d%02dZ)(!(ipatokenNotAfter=*)))"
    "(|(ipatokenDisabled=FALSE)(!(ipatokenDisabled=*)))";
    char actfilt[sizeof(template)];
    struct tm tm;
    time_t now;

    if (!active)
        return find(id, user_dn, token_dn, NULL, filter);

    /* Get the current time. */
    if (time(&now) == (time_t) -1)
        return NULL;
    if (gmtime_r(&now, &tm) == NULL)
        return NULL;

    /* Get the current time string. */
    if (snprintf(actfilt, sizeof(actfilt), template,
                 tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                 tm.tm_hour, tm.tm_min, tm.tm_sec,
                 tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                 tm.tm_hour, tm.tm_min, tm.tm_sec) < 0)
        return NULL;

    return find(id, user_dn, token_dn, actfilt, filter);
}

int otptoken_get_digits(struct otptoken *token)
{
    return token == NULL ? 0 : token->token.digits;
}

const Slapi_DN *otptoken_get_sdn(struct otptoken *token)
{
    return token->sdn;
}

static bool otptoken_validate(struct otptoken *token, size_t steps,
                              uint32_t code)
{
    time_t now = 0;

    if (token == NULL)
        return false;

    /* We only need the local time for time-based tokens. */
    if (token->type == OTPTOKEN_TOTP && time(&now) == (time_t) -1)
        return false;

    for (int i = 0; i <= steps; i++) {
        /* Validate the positive step. */
        if (validate(token, now, i, code, NULL))
            return true;

        /* Validate the negative step. */
        if (validate(token, now, 0 - i, code, NULL))
            return true;
    }

    return false;
}


/*
 *  Convert code berval to decimal.
 *
 *  NOTE: We can't use atol() or strtoul() because:
 *    1. If we have leading zeros, atol() fails.
 *    2. Neither support limiting conversion by length.
 */
static bool bvtod(const struct berval *code, uint32_t *out)
{
    *out = 0;

    for (ber_len_t i = 0; i < code->bv_len; i++) {
        if (code->bv_val[i] < '0' || code->bv_val[i] > '9')
            return false;
        *out *= 10;
        *out += code->bv_val[i] - '0';
    }

    return code->bv_len != 0;
}

bool otptoken_validate_berval(struct otptoken *token, size_t steps,
                              const struct berval *code, bool tail)
{
    struct berval tmp;
    uint32_t otp;

    if (token == NULL || code == NULL)
        return false;
    tmp = *code;

    if (tmp.bv_len < token->token.digits)
        return false;

    if (tail)
        tmp.bv_val = &tmp.bv_val[tmp.bv_len - token->token.digits];
    tmp.bv_len = token->token.digits;

    if (!bvtod(&tmp, &otp))
        return false;

    return otptoken_validate(token, steps, otp);
}

static bool otptoken_sync(struct otptoken * const *tokens, size_t steps,
                          uint32_t first_code, uint32_t second_code)
{
    time_t now = 0;

    if (tokens == NULL)
        return false;

    if (time(&now) == (time_t) -1)
        return false;

    for (int i = 0; i <= steps; i++) {
        for (int j = 0; tokens[j] != NULL; j++) {
            /* Validate the positive step. */
            if (validate(tokens[j], now, i, first_code, &second_code))
                return true;

            /* Validate the negative step. */
            if (validate(tokens[j], now, 0 - i, first_code, &second_code))
                return true;
        }
    }

    return false;
}

bool otptoken_sync_berval(struct otptoken * const *tokens, size_t steps,
                          const struct berval *first_code,
                          const struct berval *second_code)
{
    uint32_t second = 0;
    uint32_t first = 0;

    if (!bvtod(first_code, &first))
        return false;

    if (!bvtod(second_code, &second))
        return false;

    return otptoken_sync(tokens, steps, first, second);
}
