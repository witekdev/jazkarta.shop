import hmac
import time
import random
from hashlib import md5
from persistent.mapping import PersistentMapping
from premailer import Premailer
from ZODB.POSException import ConflictError
from zope.browserpage import ViewPageTemplateFile
from zope.cachedescriptors.property import Lazy as lazy_property
from jazkarta.shop import config
from jazkarta.shop import storage
from . import CheckoutFormBase
from ...cart import Cart
from ...interfaces import IPurchaseHandler
from ...utils import get_current_userid
from ...utils import get_setting
from ...utils import resolve_uid
from ...utils import run_in_transaction
from ...utils import send_mail
from ...validators import is_email


class CheckoutFormAuthorizeNetSIM(CheckoutFormBase):
    """ Renders a checkout form with button to submit to Authorize.Net SIM """
    index = ViewPageTemplateFile(
        '../templates/checkout_form_authorize_net_sim.pt')

    # Note: Customer only has 15min to complete the checkout process as per:
    # https://support.authorize.net/authkb/index?page=content&id=A592&actp=LIST

    def __call__(self):
        if 'x_response_code' in self.request.form:
            # recreate the cart from storage (by user_id or browser_id)
            user_id = self.request.form.get('user_id', None)
            browser_id = self.request.form.get('browser_id', None)
            self.cart = Cart.from_request(
                self.request, user_id=user_id, browser_id=browser_id)
        self.update()
        if 'x_response_code' in self.request.form:
            self.handle_submit()
        return self.render()

    def render(self):
        if 'x_response_code' in self.request.form:
            return self.thankyou_page()
        else:
            return self.index()

    @lazy_property
    def post_url(self):
        if config.IN_PRODUCTION:
            return get_setting('authorizenet_sim_url_production')
        else:
            return get_setting('authorizenet_sim_url_dev')

    @lazy_property
    def x_login(self):
        if config.IN_PRODUCTION:
            return get_setting('authorizenet_api_login_id_production')
        else:
            return get_setting('authorizenet_api_login_id_dev')

    @lazy_property
    def transaction_key(self):
        if config.IN_PRODUCTION:
            return get_setting('authorizenet_transaction_key_production')
        else:
            return get_setting('authorizenet_transaction_key_dev')

    @lazy_property
    def x_fp_hash(self):
        """
        x_fp_hash Required.
        Value: The unique transaction fingerprint.
        Notes: The fingerprint is generated using the HMAC-MD5 hashing algorithm
        on the following field values:
        API login ID (x_login)
        The sequence number of the transaction (x_fp_sequence)
        The timestamp of the sequence number creation (x_fp_timestamp)
        Amount (x_amount)
        Currency code, if submitted (x_currency_code)
        Field values are concatenated and separated by a caret (^).

        Also as per:
        https://support.authorize.net/authkb/index?page=content&id=A569
        trailing ^ is required!!
        APIl0gin1D^Sequence123^1457632735^19.99^
        """
        values = (str(self.x_login), self.x_fp_sequence,
             self.x_fp_timestamp, str(self.amount))
        source = "^".join(values) + '^'
        hashed_values = hmac.new(str(self.transaction_key), '', md5)
        hashed_values.update(source) # add content
        return hashed_values.hexdigest()

    @lazy_property
    def x_fp_sequence(self):
        """
        x_fp_sequence Required
        Value: The merchant-assigned sequence number for the transaction.
        Format: Numeric.
        Notes: The sequence number can be a merchant-assigned value, such as an
        invoice number or any randomly generated number.
        """
        return str(int(random.random() * 100000000000))

    @lazy_property
    def x_fp_timestamp(self):
        """
        x_fp_timestamp Required
        Value: The timestamp at the time of fingerprint generation.
        Format: UTC time in seconds since January 1, 1970.
        Notes: Coordinated Universal Time (UTC) is an international atomic
        standard of time
        (sometimes referred to as GMT). Using a local time zone timestamp causes
        fingerprint authentication to fail.

        In case of error code 97
        For debugging local time vs authorize.net server time
        http://developer.authorize.net/api/reference/responseCode97.html
        """
        return str(int(time.time()))

    @lazy_property
    def browser_id(self):
        return self.context.browser_id_manager.getBrowserId()

    @lazy_property
    def user_id(self):
        userid = get_current_userid()
        if userid is not None:
            return userid
        return None

    @lazy_property
    def x_relay_url(self):
        return self.context.absolute_url() + '/checkout'

        # to debug use http://developer.authorize.net/bin/developer/paramdump
        # and to prevent this error:
        # (14) The referrer, relay response or receipt link URL is invalid.
        # return "http://developer.authorize.net/bin/developer/paramdump"
        # also useful:
        # https://support.authorize.net/authkb/index?page=content&id=A663&pmv=
        # print&impressions=false

    @lazy_property
    def x_cancel_url(self):
        # for optional cancel button on SIM form. Take the user to the homepage
        return self.context.absolute_url()

    @lazy_property
    def sim_logo_url(self):
        # for optional logo image hosted by authorize.net servers
        return get_setting('authorizenet_sim_logo_url')

    def handle_submit(self):
        amount = self.amount
        response = self.request['x_response_code']
        if response != '1':
            # Authorize.Net SIM response codes
            # 1 = Approved
            # 2 = Declined
            # 3 = Error
            # 4 = Held for Review
            if 'x_response_reason_text' in self.request.form:
                self.error = self.request['x_response_reason_text']
            else:
                # this scenario unlikely as every response should provide us
                # with x_response_reason_text
                self.error = ('There was an error with your transaction. '
                              'Your payment has not been processed. '
                              'Please contact us for assistance.')
        if self.error:
            return

        userid = get_current_userid()
        contact_info = PersistentMapping()
        if 'x_first_name' in self.request.form:
            contact_info['first_name'] = self.request.form['x_first_name']
        if 'x_last_name' in self.request.form:
            contact_info['last_name'] = self.request.form['x_last_name']
        if 'x_email' in self.request.form:
            contact_info['email'] = self.request.form['x_email']
        if 'x_phone' in self.request.form:
            contact_info['phone'] = self.request.form['x_phone']
        if 'x_address' in self.request.form:
            contact_info['address'] = self.request.form['x_address']
        if 'x_city' in self.request.form:
            contact_info['city'] = self.request.form['x_city']
        if 'x_state' in self.request.form:
            contact_info['state'] = self.request.form['x_state']
        if 'x_zip' in self.request.form:
            contact_info['zip'] = self.request.form['x_zip']
        if 'x_country' in self.request.form:
            contact_info['country'] = self.request.form['x_country']

        method = 'Online Payment'

        if not self.error:
            try:
                self.store_order(method, userid, contact_info)
                self.notify_purchased()
                self.clear_cart()
            except ConflictError:
                self.error = ('Failed to store results of payment after '
                              'multiple retries. Please contact us for '
                              'assistance. ')

    # This code runs after the payment is processed
    # to update various data in the ZODB.
    @run_in_transaction(retries=10)
    def store_order(self, method, userid, contact_info):
        """Store various data following payment processing.

        This code runs in a separate transaction (with up to 10 retries)
        to make sure that ConflictErrors do not cause the Zope publisher
        to retry the request which performs payment processing.

        IMPORTANT: Make sure that code which runs before this is called
        does not try to make persistent changes, because they'll be lost.
        """
        order = self.cart.data
        order['bill_to'] = contact_info
        order['notes'] = self.request.form.get('notes')

        # Call `after_purchase` hook for each product
        coupons_used = set()
        for index, item in enumerate(self.cart.items):
            ob = resolve_uid(item.uid)
            if ob is not None:
                purchase_handler = IPurchaseHandler(ob)
                purchase_handler.after_purchase(item._item)

            # keep track of coupons used
            if item.is_discounted:
                coupons_used.add(item.coupon)

        # store count of coupon usage
        for coupon_uid in coupons_used:
            storage.increment_shop_data(
                [userid, 'coupons', coupon_uid], 1)

        # Store historic record of order
        self.order_id = self.cart.store_order(userid)

        # Keep cart as old_cart (for the thank you page) before clearing it
        self.old_cart = self.cart.clone()
        self.cart_is_editable = False

        # Queue receipt email (email is actually sent at transaction commit)
        if self.receipt_email:
            subject = get_setting('receipt_subject')
            unstyled_msg = self.receipt_email()
            css = self.context.unrestrictedTraverse('plone.css')()
            # remove specific lines of css that were causing premailer to fail
            # due to:'unicodeescape' codec can't decode byte 0x5c in position 26
            # x--------------------------
            css_parsed = []
            for x in css.split("\n"):
                if '\.' not in x:
                    css_parsed.append(x)
            cssp = "".join(css_parsed)
            # x--------------------------
            msg = Premailer(unstyled_msg, css_text=cssp).transform()
            # only send email if the email field was entered in the SIM billing
            # address section and if the email address is a valid email format
            if 'x_email' in self.request.form:
                try:
                    is_email(self.request.form['x_email'])
                except Exception as e:
                    self.mail_not_sent = (
                        'Receipt email was not sent as the '
                        'email address entered on the payment '
                        'form was not valid.')
                if not self.mail_not_sent:
                    mto = self.request['x_email']
                    send_mail(subject, msg, mto=mto)