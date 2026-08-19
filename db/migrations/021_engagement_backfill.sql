-- Rows logged before reply-tracking existed have replied = null because nothing ever
-- watched for their replies — harvesting them as "no reply" would fabricate negative
-- engagement examples out of ignorance. Mark history as already harvested; only messages
-- sent under tracking become training data.

update proactive_log set harvested = true where replied is null and not harvested;
